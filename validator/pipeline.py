"""Конвейер проверки.

  1. Чтение документа и маскирование чувствительных данных.
  2. LLM: нарезка на требования; цитаты привязываются к строкам документа.
  3. Правила: детерминированные проверки.
  4. LLM: проверка каждого требования по 9 критериям (пакетами).
  5. LLM: проверка набора: противоречия; полнота и прочие свойства набора.
  6. Привязка цитат всех находок; находки без опоры на текст отбрасываются.
  7. LLM: независимая верификация, калибровка серьёзности, удаление дублей.
  8. LLM: вопросы для уточнения.
  9. Решение о готовности (правилом) и итог (LLM).
 10. Отчёт.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__, prompts, rules
from .config import Config
from .criteria import ALL, REQUIREMENT_CRITERIA, SET_CRITERIA, SEVERITIES
from .grounding import Anchor, Grounder
from .llm import GeminiClient
from .loader import Document, load
from .redact import Redactor
from .schemas import (
    Clarification,
    Conclusion,
    Finding,
    Requirement,
    RequirementBatch,
    Segmentation,
    SetReview,
    Verification,
)

SEV_ORDER = {"critical": 0, "major": 1, "minor": 2}


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
    client: GeminiClient | None = None
    audit_dir: Path | None = None

    def kept(self) -> list[Finding]:
        return [f for f in self.findings if f.kept and not f.duplicate_of]

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


def run(input_path: str, cfg: Config, log=print, dry_run: bool = False, client_factory=None) -> RunResult:
    """client_factory(redactor, audit_dir): свой клиент модели (для тестов); по умолчанию GeminiClient."""
    started = datetime.now(cfg.tz)
    doc = load(input_path)
    res = RunResult(doc=doc, config=cfg, started=started, agent_version=agent_version())
    log(f"Документ: {doc.path.name} ({doc.fmt}, {len(doc.lines)} строк), «{doc.title}»")

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

    audit = Path(cfg.runs_dir) / started.strftime("%Y%m%d-%H%M%S")
    audit.mkdir(parents=True, exist_ok=True)
    res.audit_dir = audit
    (audit / "masked_input.md").write_text(masked, encoding="utf-8")

    if dry_run:
        redactor.guard(prompts.SYSTEM + prompts.segmentation(numbered))
        (audit / "01_segment.request.md").write_text(
            f"# SYSTEM\n\n{prompts.SYSTEM}\n\n# USER\n\n{prompts.segmentation(numbered)}", encoding="utf-8"
        )
        log(f"Сухой прогон: в модель ничего не отправлено. Что ушло бы: {audit}")
        return res

    if client_factory is None:
        client = GeminiClient(cfg.api_key, cfg.model, redactor, audit, cfg.temperature, log=log, fallbacks=cfg.fallbacks)
    else:
        client = client_factory(redactor, audit)
    res.client = client

    # 2. нарезка
    t = _step(log, "Нарезка документа на требования")
    seg = client.generate("segment", prompts.SYSTEM, prompts.segmentation(numbered), Segmentation)
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
    t = _step(log, "Детерминированные проверки")
    raw: list[Finding] = []
    for h in rules.run(doc, masked):
        anchors = [grounder.line_anchor(ln) for ln in h.lines]
        req_ids = [] if h.whole_doc else _reqs_for_lines(res, h.lines)
        scope = "requirement" if (req_ids and not ALL[h.criterion].code.startswith("set_")) else "set"
        raw.append(Finding("", scope, h.criterion, h.severity, req_ids, anchors, h.problem, h.fix, "rule"))
    _done(log, t, f"{len(raw)} находок")

    # 4. проверка каждого требования
    bs = cfg.batch_size
    batches = [res.requirements[i : i + bs] for i in range(0, len(res.requirements), bs)]
    for bi, batch in enumerate(batches, 1):
        t = _step(log, f"Проверка требований, пакет {bi}/{len(batches)} ({batch[0].rid}…{batch[-1].rid})")
        payload = [
            {"rid": r.rid, "doc_id": r.doc_id, "section": r.anchor.section or r.section,
             "lines": f"{r.anchor.line}-{r.anchor.line_end}", "quote": r.quote}
            for r in batch
        ]
        out = client.generate(f"review_{bi}", prompts.SYSTEM, prompts.requirement_review(numbered, payload), RequirementBatch)
        n = 0
        for rv in out.reviews:
            rid = _norm_rid(rv.req_id)
            if not res.req(rid):
                continue
            res.req_verdicts[rid] = {
                v.criterion: (v.verdict.strip().lower(), v.comment) for v in rv.verdicts if v.criterion in ALL
            }
            for f in rv.findings:
                raw.append(_finding_from_llm("requirement", f.criterion, f.severity, [rid], f.quotes, f.problem, f.fix, grounder))
                n += 1
        _done(log, t, f"{n} находок")

    # 5. проверка набора
    known = "\n".join(f"- {f.problem}" for f in raw if f.source == "rule") or "(нет)"
    table = _req_table(res)
    for name, builder in (("set_consistency", prompts.set_consistency), ("set_completeness", prompts.set_completeness)):
        t = _step(log, "Проверка набора: " + ("противоречия" if name == "set_consistency" else "полнота и свойства набора"))
        out = client.generate(name, prompts.SYSTEM, builder(numbered, table, known), SetReview)
        for v in out.verdicts:
            if v.criterion in ALL:
                res.set_verdicts[v.criterion] = (v.verdict.strip().lower(), v.comment)
        for f in out.findings:
            req_ids = [r for r in (_norm_rid(x) for x in f.req_ids) if res.req(r)]
            crit = f.criterion if f.criterion in ALL else "set_consistent" if name == "set_consistency" else "set_complete"
            raw.append(_finding_from_llm("set", crit, f.severity, req_ids, f.quotes, f.problem, f.fix, grounder))
        _done(log, t, f"{len(out.findings)} находок")

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
    t = _step(log, f"Верификация {len(res.findings)} находок")
    ver = client.generate("verify", prompts.SYSTEM, prompts.verification(numbered, _findings_block(res.findings)), Verification)
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
    kept = res.kept()
    _done(log, t, f"подтверждено {len(kept)}, отклонено {sum(1 for f in res.findings if not f.kept)}, "
          f"дублей {sum(1 for f in res.findings if f.kept and f.duplicate_of)}")
    _reconcile_verdicts(res)

    # 8. вопросы
    t = _step(log, "Вопросы для уточнения")
    important = [f for f in kept if f.severity in ("critical", "major")] or kept
    cl = client.generate("clarify", prompts.SYSTEM, prompts.clarification(numbered, _findings_block(important)), Clarification)
    order = {"blocking": 0, "important": 1, "nice": 2}
    res.questions = sorted(cl.questions, key=lambda q: order.get(q.priority, 3))
    _done(log, t, f"{len(res.questions)} вопросов")

    # 9. итог
    t = _step(log, "Итог")
    _decide(res)
    stats_txt = _stats_text(res)
    decision = f"Разработка: {res.decision_dev}\nТестирование: {res.decision_test}"
    res.conclusion = client.generate(
        "conclusion", prompts.SYSTEM, prompts.conclusion(stats_txt, decision, _findings_block(important)), Conclusion
    )
    _done(log, t, "")
    res.finished = datetime.now(cfg.tz)
    save_json(res, audit / "result.json")
    return res


def save_json(res: RunResult, path: Path) -> None:
    """Структурированный результат прогона (без расшифровки маскирования)."""
    import json

    def anchor(a: Anchor) -> dict:
        return {"text": a.text, "line": a.line, "line_end": a.line_end, "section": a.section, "page": a.page}

    data = {
        "document": {"path": str(res.doc.path), "title": res.doc.title, "sha256": res.doc.sha256},
        "model": res.config.model,
        "model_versions": sorted(res.client.usage.model_versions) if res.client else [],
        "steps": list(res.client.usage.steps) if res.client else [],
        "agent_version": res.agent_version,
        "started": res.started.isoformat(),
        "requirements": [
            {"rid": r.rid, "doc_id": r.doc_id, "kind": r.kind, "summary": r.summary, "anchor": anchor(r.anchor)}
            for r in res.requirements
        ],
        "findings": [
            {"fid": f.fid, "scope": f.scope, "criterion": f.criterion, "severity": f.severity, "req_ids": f.req_ids,
             "source": f.source, "kept": f.kept, "duplicate_of": f.duplicate_of, "problem": f.problem, "fix": f.fix,
             "verifier_note": f.verifier_note, "anchors": [anchor(a) for a in f.anchors]}
            for f in res.findings
        ],
        "ungrounded": [{"criterion": f.criterion, "problem": f.problem, "quotes": f.dropped_quotes} for f in res.ungrounded],
        "questions": [q.model_dump() for q in res.questions],
        "decision": {"development": res.decision_dev, "testing": res.decision_test},
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- вспомогательные ----------


def _numbered(text: str) -> str:
    lines = text.split("\n")
    w = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{w}}| {ln}" for i, ln in enumerate(lines))


def _step(log, title: str) -> float:
    log(f"▸ {title}…")
    return time.time()


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


def _finding_from_llm(scope, criterion, severity, req_ids, quotes, problem, fix, grounder: Grounder) -> Finding:
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
    if crit not in ALL:
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
        for c in REQUIREMENT_CRITERIA:
            v, comment = cur.get(c.code, ("ok", ""))
            ids = sorted(set(viol.get((r.rid, c.code), [])))
            if ids:
                cur[c.code] = ("violation", comment if v == "violation" else ", ".join(ids))
            elif v == "violation":
                cur[c.code] = ("ok", "нарушение не подтверждено верификатором")
            elif v not in ("ok", "na"):
                cur[c.code] = ("ok", comment)
    for c in SET_CRITERIA:
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
    kept = res.kept()
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


def _stats_text(res: RunResult) -> str:
    kept = res.kept()
    by_sev = {s: sum(1 for f in kept if f.severity == s) for s in SEVERITIES}
    return (
        f"Требований: {len(res.requirements)}. Подтверждённых нарушений: {len(kept)} "
        f"(критичных {by_sev['critical']}, существенных {by_sev['major']}, незначительных {by_sev['minor']})."
    )


def _req_table(res: RunResult) -> str:
    return "\n".join(
        f"{r.rid} | {r.doc_id} | {r.anchor.line}-{r.anchor.line_end} | {r.summary}" for r in res.requirements
    )


def _findings_block(findings: list[Finding]) -> str:
    out = []
    for f in findings:
        quotes = "; ".join(f"«{a.text}» (строка {a.line})" for a in f.anchors)
        out.append(
            f"{f.fid} [{f.source}] критерий={f.criterion} ({ALL[f.criterion].name}) серьёзность={f.severity} "
            f"ТР: {', '.join(f.req_ids) or 'документ'}\n"
            f"  цитаты: {quotes}\n  проблема: {f.problem}\n  исправление: {f.fix}"
        )
    return "\n".join(out)
