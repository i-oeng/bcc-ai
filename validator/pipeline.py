"""Конвейер проверки.

  1. Чтение документа и маскирование чувствительных данных.
  2. LLM: нарезка на требования; цитаты привязываются к строкам документа.
  3. Правила: детерминированные проверки.
  4. LLM: проверка каждого требования по критериям выбранных стандартов (пакетами).
  5. LLM: проверка набора: противоречия; полнота и прочие свойства набора.
     Шаги 4 и 5 независимы и идут параллельно, как и итог с исправленной редакцией.
  6. Привязка цитат всех находок; находки без опоры на текст отбрасываются.
  7. LLM: независимая верификация, калибровка серьёзности, удаление дублей.
  8. LLM: вопросы для уточнения.
  9. Решение о готовности (правилом) и итог (LLM).
 10. Отчёт.
"""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__, prompts, rules
from .config import Config
from .criteria import DEFAULT, SEVERITIES, Profile, profile as make_profile
from .grounding import Anchor, Grounder
from .llm import Cancelled, LLMError, make_client
from .loader import Document, load
from .redact import Redactor
from .schemas import (
    Clarification,
    Conclusion,
    AnswerReview,
    CompletenessReview,
    Dedup,
    Final,
    FixedDocument,
    Finding,
    Requirement,
    RequirementBatch,
    Segmentation,
    SetReview,
    Verification,
)

SEV_ORDER = {"critical": 0, "major": 1, "minor": 2}
DEDUPE_THRESHOLD = 45


@dataclass
class RunResult:
    doc: Document
    config: Config
    started: datetime
    finished: datetime | None = None
    agent_version: str = ""
    requirements: list[Requirement] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    lost_requirements: list[str] = field(default_factory=list)  # цитаты, не найденные в документе
    findings: list[Finding] = field(default_factory=list)
    ungrounded: list[Finding] = field(default_factory=list)
    req_verdicts: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)  # rid -> code -> (verdict, comment)
    set_verdicts: dict[str, tuple[str, str]] = field(default_factory=dict)
    questions: list = field(default_factory=list)
    conclusion: Conclusion | None = None
    decision_dev: str = ""
    decision_test: str = ""
    redactor: Redactor | None = None
    usage: object = None  # llm.Usage
    warnings: list[str] = field(default_factory=list)
    coverage: list = field(default_factory=list)  # CoverageRow
    checklist_items: list[str] = field(default_factory=list)
    checklist: list = field(default_factory=list)  # ChecklistAnswer
    answers: list = field(default_factory=list)  # clarify.QA
    fixed: FixedDocument | None = None
    audit_dir: Path | None = None
    profile: Profile | None = None  # стандарты проверки и их критерии

    @property
    def prof(self) -> Profile:
        return getattr(self, "profile", None) or DEFAULT  # у сохранённых старых проверок профиля нет

    def kept(self) -> list[Finding]:
        return [f for f in self.findings if f.kept and not f.duplicate_of]

    def open_findings(self) -> list[Finding]:
        """Подтверждённые и не закрытые ответами заказчика."""
        return [f for f in self.kept() if f.resolution != "closed"]

    def req(self, rid: str) -> Requirement | None:
        return next((r for r in self.requirements if r.rid == rid), None)


def agent_version() -> str:
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return f"{__version__} (git {out.stdout.strip()})"
    except Exception:
        pass
    return __version__


def run(
    input_path: str,
    cfg: Config,
    log=print,
    dry_run: bool = False,
    client_factory=None,
    answers_path: str | None = None,
    fix: bool = False,
    should_stop=None,
) -> RunResult:
    """client_factory(redactor, audit_dir): свой клиент модели (для тестов); по умолчанию по cfg.provider.

    should_stop(): True, если пользователь остановил проверку; тогда поднимается llm.Cancelled.
    """
    started = datetime.now(cfg.tz)
    stop = should_stop or (lambda: False)
    prof = make_profile(cfg.standards)
    doc = load(input_path)
    res = RunResult(doc=doc, config=cfg, started=started, agent_version=agent_version(), profile=prof)
    log(f"Документ: {doc.path.name} ({doc.fmt}, {len(doc.lines)} строк), «{doc.title}»")
    log(f"Стандарты: {prof.titles()}")
    SYSTEM = prompts.system(prof)

    # 1. маскирование
    redactor = Redactor.from_env(cfg.redact, cfg.redact_ner, cfg.redact_terms_file)
    masked = redactor.redact(doc.text)
    if masked.count("\n") != doc.text.count("\n"):
        raise RuntimeError("Маскирование изменило число строк документа")
    res.redactor = redactor
    stats = redactor.stats()
    log("Маскирование: " + (", ".join(f"{k}: {v}" for k, v in stats.items()) if stats else "чувствительных данных не найдено")
        + ("" if cfg.redact else " (ОТКЛЮЧЕНО)"))
    grounder = Grounder(doc, masked)
    numbered = _numbered(masked)

    stamp = started.strftime("%Y%m%d-%H%M%S")
    audit, n = Path(cfg.runs_dir) / stamp, 1
    while audit.exists():  # две проверки в одну секунду не должны затирать друг друга
        n += 1
        audit = Path(cfg.runs_dir) / f"{stamp}-{n}"
    try:
        audit.mkdir(parents=True)
    except FileExistsError:  # параллельная проверка заняла то же имя
        audit = Path(cfg.runs_dir) / f"{stamp}-{time.time_ns() % 100000}"
        audit.mkdir(parents=True)
    res.audit_dir = audit
    (audit / "masked_input.md").write_text(masked, encoding="utf-8")

    if dry_run:
        redactor.guard(SYSTEM + prompts.segmentation(numbered))
        (audit / "01_segment.request.md").write_text(
            f"# SYSTEM\n\n{SYSTEM}\n\n# USER\n\n{prompts.segmentation(numbered)}", encoding="utf-8"
        )
        log(f"Сухая проверка: в модель ничего не отправлено. Что ушло бы: {audit}")
        return res

    if client_factory is None:
        client = make_client(cfg, redactor, audit, log=log, should_stop=stop)
    else:
        client = client_factory(redactor, audit)
    res.usage = client.usage
    log(f"Модель: {cfg.provider}, {cfg.model}" + (f" (запасные: {', '.join(cfg.fallbacks)})" if cfg.fallbacks else ""))

    # 2. нарезка
    t = _step(log, "Нарезка документа на требования", stop)
    seg = client.generate("segment", SYSTEM, prompts.segmentation(numbered), Segmentation)
    for r in seg.requirements:
        a = grounder.find(r.quote, min_score=85)
        if not a:
            res.lost_requirements.append(f"{r.doc_id}: {r.quote}")
            continue
        res.requirements.append(
            Requirement(rid="", doc_id=r.doc_id.strip() or "без номера", section=r.section, summary=r.summary,
                        kind=r.kind, anchor=a, quote=a.text)
        )
    res.requirements.sort(key=lambda r: (r.anchor.line, r.anchor.line_end))
    _dedupe_requirements(res)
    for i, r in enumerate(res.requirements, 1):
        r.rid = f"ТР-{i:02d}"
    res.skipped = [(s.section, s.reason) for s in seg.skipped]
    _done(log, t, f"{len(res.requirements)} требований"
          + (f", {len(res.lost_requirements)} отброшено (цитата не найдена)" if res.lost_requirements else ""))
    if not res.requirements:
        raise RuntimeError("Модель не выделила ни одного требования с цитатой из документа")

    # 3. правила
    t = _step(log, "Детерминированные проверки", stop)
    raw: list[Finding] = []
    for h in rules.run(doc, masked):
        if not prof.has(h.criterion):
            continue  # критерий из стандарта, который не выбран (например, ранжирование из IEEE 830)
        anchors = [grounder.line_anchor(ln) for ln in h.lines]
        req_ids = [] if h.whole_doc else _reqs_for_lines(res, h.lines)
        scope = "requirement" if (req_ids and not h.criterion.startswith("set_")) else "set"
        raw.append(Finding("", scope, h.criterion, h.severity, req_ids, anchors, h.problem, h.fix, "rule"))
    _done(log, t, f"{len(raw)} находок")

    # 4-5. проверка требований пакетами и два прохода по набору: шаги независимы, идут параллельно
    bs = max(1, cfg.batch_size)
    batches = [res.requirements[i : i + bs] for i in range(0, len(res.requirements), bs)]
    known = "\n".join(f"- {f.problem}" for f in raw if f.source == "rule") or "(нет)"
    table = _req_table(res)
    tasks: list[tuple[str, object]] = []
    for bi, batch in enumerate(batches, 1):
        payload = [
            {"rid": r.rid, "doc_id": r.doc_id, "section": r.anchor.section or r.section,
             "lines": f"{r.anchor.line}-{r.anchor.line_end}", "quote": r.quote}
            for r in batch
        ]
        tasks.append((f"review_{bi}", lambda p=payload, bi=bi: client.generate(
            f"review_{bi}", SYSTEM, prompts.requirement_review(numbered, p, prof), RequirementBatch)))
    tasks.append(("set_consistency", lambda: client.generate(
        "set_consistency", SYSTEM, prompts.set_consistency(numbered, table, known), SetReview)))
    tasks.append(("set_completeness", lambda: client.generate(
        "set_completeness", SYSTEM, prompts.set_completeness(numbered, table, known, prof), CompletenessReview)))
    t = _step(log, f"Проверка требований ({len(batches)} пакетов) и набора (2 прохода), параллельно", stop)
    outs = _parallel(tasks, cfg.parallel, stop)

    n_req = 0
    for bi in range(1, len(batches) + 1):
        for rv in outs[f"review_{bi}"].reviews:
            rid = _norm_rid(rv.req_id)
            if not res.req(rid):
                continue
            res.req_verdicts[rid] = {
                v.criterion: (v.verdict.strip().lower(), v.comment) for v in rv.verdicts if prof.has(v.criterion)
            }
            for f in rv.findings:
                raw.append(_finding_from_llm("requirement", f.criterion, f.severity, [rid], f.quotes, f.problem, f.fix,
                                             grounder, prof))
                n_req += 1
    n_set = 0
    for name in ("set_consistency", "set_completeness"):
        out = outs[name]
        if isinstance(out, CompletenessReview):
            res.coverage = [c for c in out.coverage if res.req(_norm_rid(c.req_id))]
            for c in res.coverage:
                c.req_id = _norm_rid(c.req_id)
                c.acceptance_ids = [a for a in (_norm_rid(x) for x in c.acceptance_ids) if res.req(a)]
        for v in out.verdicts:
            if prof.has(v.criterion):
                res.set_verdicts[v.criterion] = (v.verdict.strip().lower(), v.comment)
        for f in out.findings:
            req_ids = [r for r in (_norm_rid(x) for x in f.req_ids) if res.req(r)]
            crit = f.criterion if prof.has(f.criterion) else "set_consistent" if name == "set_consistency" else "set_complete"
            raw.append(_finding_from_llm("set", crit, f.severity, req_ids, f.quotes, f.problem, f.fix, grounder, prof))
            n_set += 1
    _done(log, t, f"по требованиям {n_req} находок, по набору {n_set}")

    # 6. привязка к тексту
    for f in raw:
        if not f.anchors:
            res.ungrounded.append(f)
        else:
            if not f.req_ids:
                f.req_ids = _reqs_for_lines(res, [a.line for a in f.anchors])
                if f.scope == "requirement" and not f.req_ids:
                    f.scope = "set"
            res.findings.append(f)
    _number_findings(res)
    if res.ungrounded:
        log(f"  отброшено находок без опоры на текст документа: {len(res.ungrounded)}")

    # 7. верификация
    t = _step(log, f"Верификация {len(res.findings)} находок", stop)
    by_line = sorted(res.findings, key=lambda f: (f.anchors[0].line, f.fid))
    ver = client.generate("verify", SYSTEM, prompts.verification(numbered, _findings_block(by_line)), Verification)
    by_id = {f.fid: f for f in res.findings}
    for it in ver.items:
        f = by_id.get(it.finding_id.strip())
        if not f:
            continue
        f.kept = bool(it.keep)
        if it.severity in SEVERITIES:
            f.severity = it.severity
        dup = it.duplicate_of.strip()
        if dup and dup in by_id and dup != f.fid and by_id[dup].kept is not False:
            f.duplicate_of = dup
        f.verifier_note = it.reason
    _fix_duplicate_chains(res)
    _done(log, t, f"подтверждено {len(res.kept())}, отклонено {sum(1 for f in res.findings if not f.kept)}, "
          f"дублей {sum(1 for f in res.findings if f.kept and f.duplicate_of)}")

    # 7б. отдельный проход по дублям, только если находок много (экономим квоту)
    if len(res.kept()) > DEDUPE_THRESHOLD:
        t = _step(log, f"Объединение дублей среди {len(res.kept())} находок", stop)
        cand = sorted(res.kept(), key=lambda f: (f.anchors[0].line, f.fid))
        try:
            dd = client.generate("dedupe", SYSTEM, prompts.deduplication(_findings_block(cand, brief=True)), Dedup)
            merged = _apply_dedup(res, dd)
            _done(log, t, f"объединено {merged}, осталось {len(res.kept())}")
        except LLMError as e:
            res.warnings.append(f"Проход объединения дублей не выполнен: {e}")
            log(f"  пропущено: {e}")
    _merge_duplicates_into_main(res)
    _renumber(res)
    kept = res.kept()
    _reconcile_verdicts(res)

    # 7в. ответы заказчика на вопросы прошлого прогона
    answers_txt = "(ответов пока нет)"
    if answers_path:
        from .clarify import parse_answers, qa_block

        res.answers = parse_answers(answers_path)
        log(f"Ответы заказчика: {len(res.answers)} из файла {answers_path}")
        if res.answers:
            # ответы пишет человек: они тоже проходят маскирование перед отправкой
            answers_txt = redactor.redact(qa_block(res.answers))
            t = _step(log, "Учёт ответов заказчика", stop)
            try:
                ar = client.generate(
                    "answers", SYSTEM, prompts.answer_review(numbered, answers_txt, _findings_block(kept)), AnswerReview
                )
                n_closed = _apply_resolutions(res, ar)
                _done(log, t, f"закрыто находок: {n_closed}")
            except LLMError as e:
                res.warnings.append(f"Ответы заказчика не учтены: {e}")
                log(f"  пропущено: {e}")

    # 8-9. вопросы, итог и чек-лист ревью; исправленная редакция (по запросу). Решение о готовности принимает код.
    # Оба запроса зависят только от подтверждённых находок, поэтому идут параллельно.
    _decide(res)
    res.checklist_items = rules.checklist_items(masked.split("\n"))
    open_ = res.open_findings()
    important = [f for f in open_ if f.severity in ("critical", "major")] or open_
    decision = f"Разработка: {res.decision_dev}\nТестирование: {res.decision_test}"
    checklist_txt = "\n".join(f"- {x}" for x in res.checklist_items) or "(чек-листа в документе нет)"
    tasks = [("final", lambda: client.generate(
        "final", SYSTEM,
        prompts.final(numbered, _stats_text(res), decision, _findings_block(important), checklist_txt, answers_txt),
        Final,
    ))]
    if fix:
        tasks.append(("fix", lambda: client.generate(
            "fix", SYSTEM, prompts.fix_document(numbered, _findings_block(open_), answers_txt), FixedDocument)))
    t = _step(log, "Вопросы для уточнения и итог" + (", исправленная редакция (параллельно)" if fix else ""), stop)
    outs = _parallel(tasks, cfg.parallel, stop, tolerate=True)
    fin = outs.get("final")
    if isinstance(fin, Final):
        order = {"blocking": 0, "important": 1, "nice": 2}
        res.questions = sorted(fin.questions, key=lambda q: order.get(q.priority, 3))
        res.checklist = fin.checklist if res.checklist_items else []
        res.conclusion = Conclusion(
            development_rationale=fin.development_rationale, testing_rationale=fin.testing_rationale,
            fix_first=fin.fix_first, overall=fin.overall,
        )
    else:
        res.warnings.append(f"Вопросы и итог от модели не получены, итог составлен правилом: {fin}")
        res.conclusion = _fallback_conclusion(res)
        log(f"  модель недоступна, итог составлен правилом: {fin}")
    if fix:
        fx = outs.get("fix")
        if isinstance(fx, FixedDocument):
            res.fixed = fx
        else:
            res.warnings.append(f"Исправленная редакция не сформирована: {fx}")
            log(f"  исправленная редакция пропущена: {fx}")
    _done(log, t, f"{len(res.questions)} вопросов"
          + (f"; изменений в редакции: {len(res.fixed.changes)}" if res.fixed else ""))

    res.finished = datetime.now(cfg.tz)
    save_json(res, audit / "result.json")
    save_state(res, audit / "state.pkl")
    return res


def save_state(res: RunResult, path: Path) -> None:
    """Полное состояние прогона, чтобы перерисовать отчёт без запросов к модели (--rerender).

    Внутри таблица маскирования с исходными значениями, поэтому файл остаётся только
    в локальной папке runs/ (она в .gitignore), как и сам исходный документ.
    """
    import pickle

    ner = res.redactor._ner if res.redactor else None
    if res.redactor:
        res.redactor._ner = None  # модель Natasha не сериализуется и не нужна для отчёта
    try:
        with open(path, "wb") as fh:
            pickle.dump(res, fh)
    finally:
        if res.redactor:
            res.redactor._ner = ner


def load_state(run_dir: str | Path) -> RunResult:
    import pickle

    with open(Path(run_dir) / "state.pkl", "rb") as fh:
        return pickle.load(fh)


def save_json(res: RunResult, path: Path) -> None:
    """Структурированный результат прогона (без расшифровки маскирования)."""
    import json

    def anchor(a: Anchor) -> dict:
        return {"text": a.text, "line": a.line, "line_end": a.line_end, "section": a.section, "page": a.page}

    data = {
        "document": {"path": str(res.doc.path), "title": res.doc.title, "sha256": res.doc.sha256},
        "model": res.config.model,
        "provider": getattr(res.config, "provider", "gemini"),
        "standards": res.prof.ids,
        "seconds": round((res.finished - res.started).total_seconds()) if res.finished else None,
        "model_versions": sorted(res.usage.model_versions) if res.usage else [],
        "steps": list(res.usage.steps) if res.usage else [],
        "agent_version": res.agent_version,
        "started": res.started.isoformat(),
        "requirements": [
            {"rid": r.rid, "doc_id": r.doc_id, "kind": r.kind, "summary": r.summary, "anchor": anchor(r.anchor)}
            for r in res.requirements
        ],
        "findings": [
            {"fid": f.fid, "orig_id": f.orig_id, "scope": f.scope, "criterion": f.criterion, "severity": f.severity, "req_ids": f.req_ids,
             "source": f.source, "kept": f.kept, "duplicate_of": f.duplicate_of, "problem": f.problem, "fix": f.fix,
             "verifier_note": f.verifier_note, "resolution": f.resolution, "resolution_note": f.resolution_note,
             "anchors": [anchor(a) for a in f.anchors]}
            for f in res.findings
        ],
        "ungrounded": [{"criterion": f.criterion, "problem": f.problem, "quotes": f.dropped_quotes} for f in res.ungrounded],
        "questions": [q.model_dump() for q in res.questions],
        "decision": {"development": res.decision_dev, "testing": res.decision_test},
        "warnings": res.warnings,
        "coverage": [c.model_dump() for c in res.coverage],
        "checklist": [c.model_dump() for c in res.checklist],
        "answers": [{"qid": q.qid, "question": q.question, "answer": q.answer} for q in res.answers],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- вспомогательные ----------


def _numbered(text: str) -> str:
    lines = text.split("\n")
    w = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{w}}| {ln}" for i, ln in enumerate(lines))


def _step(log, title: str, stop=None) -> float:
    if stop and stop():
        raise Cancelled("Проверка остановлена")
    log(f"▸ {title}…")
    return time.time()


def _parallel(tasks: list[tuple[str, object]], workers: int, stop, tolerate: bool = False) -> dict:
    """Выполнить независимые запросы к модели одновременно.

    Возвращает {имя: результат}. tolerate=False: первая ошибка прерывает шаг (остальные
    запросы доработают в фоне, их ответы попадут в кэш). tolerate=True: вместо результата
    кладётся текст ошибки LLMError. Остановка пользователем срабатывает сразу, не дожидаясь ответов.
    """
    out: dict = {}
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(tasks))), thread_name_prefix="llm")
    futures = {pool.submit(fn): name for name, fn in tasks}
    try:
        pending = set(futures)
        while pending:
            if stop():
                raise Cancelled("Проверка остановлена")
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_EXCEPTION)
            for fut in done:
                name = futures[fut]
                try:
                    out[name] = fut.result()
                except LLMError as e:
                    if not tolerate:
                        raise
                    out[name] = str(e)
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _done(log, t0: float, msg: str) -> None:
    log(f"  готово за {time.time() - t0:.0f} с" + (f": {msg}" if msg else ""))


def _norm_rid(x: str) -> str:
    import re

    m = re.search(r"(\d+)", x or "")
    return f"ТР-{int(m.group(1)):02d}" if m else (x or "").strip()


def _dedupe_requirements(res: RunResult) -> None:
    out: list[Requirement] = []
    for r in res.requirements:
        if out and r.anchor.line == out[-1].anchor.line and r.quote == out[-1].quote:
            continue
        out.append(r)
    res.requirements = out


def _reqs_for_lines(res: RunResult, lines: list[int]) -> list[str]:
    ids = []
    for ln in lines:
        for r in res.requirements:
            if r.anchor.line <= ln <= r.anchor.line_end and r.rid not in ids:
                ids.append(r.rid)
    return ids


def _finding_from_llm(scope, criterion, severity, req_ids, quotes, problem, fix, grounder: Grounder,
                     prof: Profile = DEFAULT) -> Finding:
    anchors: list[Anchor] = []
    dropped: list[str] = []
    for q in quotes:
        a = grounder.find(q)
        if a and all(a.line != b.line or a.text != b.text for b in anchors):
            anchors.append(a)
        elif not a:
            dropped.append(q)
    sev = severity.strip().lower()
    if sev not in SEVERITIES:
        sev = "major"
    crit = criterion.strip()
    if not prof.has(crit):
        crit = "complete" if scope == "requirement" else "set_complete"
    f = Finding("", scope, crit, sev, list(req_ids), anchors, problem.strip(), fix.strip(), "llm")
    f.dropped_quotes = dropped
    return f


def _number_findings(res: RunResult) -> None:
    rank = {r.rid: i for i, r in enumerate(res.requirements)}

    def key(f: Finding):
        first = min((rank.get(r, 999) for r in f.req_ids), default=999)
        return (0 if f.scope == "requirement" else 1, first, SEV_ORDER[f.severity], f.anchors[0].line)

    res.findings.sort(key=key)
    for i, f in enumerate(res.findings, 1):
        f.fid = f"Н-{i:02d}"


def _fix_duplicate_chains(res: RunResult) -> None:
    by_id = {f.fid: f for f in res.findings}
    for f in res.findings:
        if f.duplicate_of and not by_id[f.duplicate_of].kept:
            f.duplicate_of = ""  # основная находка отклонена: дубль становится самостоятельной
    for f in res.findings:
        seen = {f.fid}
        target = f.duplicate_of
        while target and by_id[target].duplicate_of and target not in seen:
            seen.add(target)
            target = by_id[target].duplicate_of
        if target in seen and target != f.duplicate_of:
            target = ""
        f.duplicate_of = target if target != f.fid else ""
    # дубль более серьёзной находки поднимает серьёзность основной
    for f in res.findings:
        if f.kept and f.duplicate_of:
            main = by_id[f.duplicate_of]
            if SEV_ORDER[f.severity] < SEV_ORDER[main.severity]:
                main.severity = f.severity
            for r in f.req_ids:
                if r not in main.req_ids:
                    main.req_ids.append(r)


def _apply_resolutions(res: RunResult, ar) -> int:
    by_id = {f.fid: f for f in res.kept()}
    closed = 0
    for r in ar.resolutions:
        f = by_id.get(r.finding_id.strip())
        st = r.status.strip().lower()
        if not f or st not in ("closed", "changed"):
            continue
        f.resolution = st
        qs = ", ".join(q for q in r.question_ids if q)
        f.resolution_note = (f"{qs}: " if qs else "") + r.note.strip()
        if st == "changed" and r.severity in SEVERITIES:
            f.severity = r.severity
        closed += st == "closed"
    return closed


def _apply_dedup(res: RunResult, dd) -> int:
    by_id = {f.fid: f for f in res.findings}
    merged = 0
    for g in dd.groups:
        main = by_id.get(g.keep.strip())
        if not main or not main.kept or main.duplicate_of:
            continue
        for d in g.duplicates:
            f = by_id.get(d.strip())
            if not f or f is main or not f.kept or f.duplicate_of:
                continue
            f.duplicate_of = main.fid
            f.verifier_note = (f.verifier_note + " " if f.verifier_note else "") + f"Дубль: {g.reason}"
            merged += 1
    _fix_duplicate_chains(res)
    return merged


def _merge_duplicates_into_main(res: RunResult) -> None:
    """Основная находка получает места и требования своих дублей."""
    by_id = {f.fid: f for f in res.findings}
    for f in res.findings:
        if not (f.kept and f.duplicate_of):
            continue
        main = by_id[f.duplicate_of]
        if SEV_ORDER[f.severity] < SEV_ORDER[main.severity]:
            main.severity = f.severity
        for r in f.req_ids:
            if r not in main.req_ids:
                main.req_ids.append(r)
        for a in f.anchors:
            if len(main.anchors) >= 4:
                break
            if all(a.line != b.line for b in main.anchors):
                main.anchors.append(a)
        main.anchors.sort(key=lambda a: a.line)
    rank = {r.rid: i for i, r in enumerate(res.requirements)}
    for f in res.findings:
        f.req_ids.sort(key=lambda r: rank.get(r, 999))


def _renumber(res: RunResult) -> None:
    """Подтверждённые находки получают сплошные номера; затем дубли и отклонённые."""
    rank = {r.rid: i for i, r in enumerate(res.requirements)}

    def key(f: Finding):
        first = min((rank.get(r, 999) for r in f.req_ids), default=999)
        return (0 if f.scope == "requirement" else 1, first, SEV_ORDER[f.severity], f.anchors[0].line)

    main = sorted([f for f in res.findings if f.kept and not f.duplicate_of], key=key)
    dups = sorted([f for f in res.findings if f.kept and f.duplicate_of], key=key)
    rej = sorted([f for f in res.findings if not f.kept], key=key)
    mapping = {}
    for i, f in enumerate(main + dups + rej, 1):
        mapping[f.fid] = f"Н-{i:02d}"
    for f in res.findings:
        f.orig_id = f.fid
        f.fid = mapping[f.fid]
    for f in res.findings:
        if f.duplicate_of:
            f.duplicate_of = mapping[f.duplicate_of]
    res.findings = main + dups + rej


def _reconcile_verdicts(res: RunResult) -> None:
    """Матрица критериев согласуется с подтверждёнными находками."""
    viol: dict[tuple[str, str], list[str]] = {}
    for f in res.findings:
        if not f.kept:
            continue
        for rid in f.req_ids:
            crit = f.criterion
            if crit.startswith("set_"):
                crit = {"set_consistent": "correct", "set_complete": "complete"}.get(crit)
                if not crit:
                    continue
            viol.setdefault((rid, crit), []).append(f.duplicate_of or f.fid)
    for r in res.requirements:
        cur = res.req_verdicts.setdefault(r.rid, {})
        for c in res.prof.requirement:
            v, comment = cur.get(c.code, ("ok", ""))
            ids = sorted(set(viol.get((r.rid, c.code), [])))
            if ids:
                cur[c.code] = ("violation", comment if v == "violation" else ", ".join(ids))
            elif v == "violation":
                cur[c.code] = ("ok", "нарушение не подтверждено верификатором")
            elif v not in ("ok", "na"):
                cur[c.code] = ("ok", comment)
    for c in res.prof.set:
        fs = [f for f in res.kept() if f.criterion == c.code]
        v, comment = res.set_verdicts.get(c.code, ("ok", ""))
        if fs:
            worst = min(SEV_ORDER[f.severity] for f in fs)
            nv = "violation" if worst <= 1 else "partial"
            if v == "violation" or nv == "violation":
                nv = "violation"
            res.set_verdicts[c.code] = (nv, comment)
        elif v not in ("ok", "partial", "violation"):
            res.set_verdicts[c.code] = ("ok", comment)


def _decide(res: RunResult) -> None:
    kept = res.open_findings()
    crit = [f for f in kept if f.severity == "critical"]
    major = [f for f in kept if f.severity == "major"]
    test_blockers = [f for f in crit if f.criterion in ("verifiable", "correct", "set_consistent", "unambiguous", "complete")]
    if crit:
        res.decision_dev = f"НЕЛЬЗЯ начинать (критичных дефектов: {len(crit)})"
    elif major:
        res.decision_dev = f"МОЖНО С ОГОВОРКАМИ (критичных дефектов нет, существенных: {len(major)})"
    else:
        res.decision_dev = "МОЖНО: критичных и существенных дефектов нет"
    if test_blockers:
        res.decision_test = f"НЕЛЬЗЯ проектировать тесты полностью (критичных дефектов, при которых ожидаемый результат не определён: {len(test_blockers)})"
    elif crit or major:
        res.decision_test = "МОЖНО С ОГОВОРКАМИ: тесты на затронутые требования придётся пересмотреть после исправлений"
    else:
        res.decision_test = "МОЖНО"


def _fallback_conclusion(res: RunResult) -> Conclusion:
    from .schemas import FixItem

    top = sorted(res.kept(), key=lambda f: (SEV_ORDER[f.severity], f.fid))[:7]
    return Conclusion(
        development_rationale=f"{res.decision_dev}.",
        testing_rationale=f"{res.decision_test}.",
        fix_first=[FixItem(title=res.prof.name(f.criterion), finding_ids=[f.fid], action=f.fix) for f in top],
        overall=_stats_text(res),
    )


def _stats_text(res: RunResult) -> str:
    kept = res.open_findings()
    by_sev = {s: sum(1 for f in kept if f.severity == s) for s in SEVERITIES}
    closed = len(res.kept()) - len(kept)
    return (
        f"Требований: {len(res.requirements)}. Открытых подтверждённых нарушений: {len(kept)} "
        f"(критичных {by_sev['critical']}, существенных {by_sev['major']}, незначительных {by_sev['minor']})"
        + (f"; закрыто ответами заказчика: {closed}." if closed else ".")
    )


def _req_table(res: RunResult) -> str:
    return "\n".join(
        f"{r.rid} | {r.doc_id} | {r.anchor.line}-{r.anchor.line_end} | {r.summary}" for r in res.requirements
    )


def _findings_block(findings: list[Finding], brief: bool = False) -> str:
    out = []
    for f in findings:
        if brief:
            lines = ", ".join(str(a.line) for a in f.anchors)
            out.append(f"{f.fid} [{f.criterion}, {f.severity}, строки {lines}, {', '.join(f.req_ids) or 'документ'}]: {f.problem}")
            continue
        quotes = "; ".join(f"«{a.text}» (строка {a.line})" for a in f.anchors)
        out.append(
            f"{f.fid} [{f.source}] критерий={f.criterion} ({DEFAULT.name(f.criterion)}) серьёзность={f.severity} "
            f"ТР: {', '.join(f.req_ids) or 'документ'}\n"
            f"  цитаты: {quotes}\n  проблема: {f.problem}\n  исправление: {f.fix}"
        )
    return "\n".join(out)
