"""Логика веб-интерфейса без привязки к фреймворку: документы, проверки, история, ответы, сравнение.

API (web/api.py) только переводит эти функции в HTTP, поэтому фронтенд можно
переделать как угодно, не трогая работу с конвейером.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path


from validator import criteria as criteria_mod
from validator import pipeline, report
from validator.clarify import write_questions
from validator.config import PROVIDERS, Config, provider_defaults
from validator.criteria import BASE_STANDARD, DEFAULT_STANDARDS, available, name as criterion_name
from validator.redact import _PLACEHOLDER

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
UPLOADS = ROOT / "runs" / "uploads"  # runs/ в .gitignore: загруженные документы не попадут в репозиторий
WEB_REPORTS = ROOT / "runs" / "web_reports"
FORMATS = ["pdf", "docx", "md", "txt", "html", "htm"]

SEV_LABEL = {"critical": "Критично", "major": "Существенно", "minor": "Незначительно"}
PRIORITY_LABEL = {"blocking": "Блокирующий", "important": "Важный", "nice": "Желательный"}


class NotFound(LookupError):
    pass


# ---------- документы ----------
# Документ в API адресуется идентификатором «sample:<имя>» или «upload:<имя>», а не путём:
# клиент не может попросить прочитать произвольный файл на диске.


def _ok_format(p: Path) -> bool:
    return p.suffix.lstrip(".").lower() in FORMATS


def samples() -> list[dict]:
    return [{"id": f"sample:{p.name}", "name": p.name} for p in sorted(SAMPLES.iterdir()) if _ok_format(p)]


def save_upload(name: str, data: bytes) -> str:
    """Загруженный файл кладём под стабильным именем: тот же файл повторно попадёт в кэш ответов модели."""
    if not _ok_format(Path(name)):
        raise ValueError(f"Неподдерживаемый формат. Допустимо: {', '.join(FORMATS)}")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", Path(name).name) or "document"
    p = UPLOADS / f"{hashlib.sha256(data).hexdigest()[:12]}_{safe}"
    if not p.exists():
        p.write_bytes(data)
    return f"upload:{p.name}"


def resolve_document(doc_id: str) -> Path:
    kind, _, name = doc_id.partition(":")
    base = {"sample": SAMPLES, "upload": UPLOADS}.get(kind)
    if base is None or not name or Path(name).name != name:
        raise NotFound(f"Документ {doc_id!r} не найден")
    p = base / name
    if not p.is_file() or not _ok_format(p):
        raise NotFound(f"Документ {doc_id!r} не найден")
    return p


def document_info(doc_id: str) -> dict:
    from validator.loader import load

    d = load(str(resolve_document(doc_id)))
    return {"id": doc_id, "name": d.path.name, "title": d.title, "format": d.fmt, "lines": len(d.lines),
            "chars": len(d.text), "sha256": d.sha256}


# ---------- настройки ----------


@dataclass
class RunOptions:
    model: str
    fallbacks: list[str]
    batch_size: int
    redact: bool
    redact_ner: bool
    use_cache: bool
    fix: bool
    provider: str = "gemini"
    base_url: str = ""
    standards: list[str] | None = None
    parallel: int = 6
    api_key: str = ""  # ключ из заголовка запроса; живёт только в памяти задачи, на диск не пишется


def default_options(provider: str | None = None) -> RunOptions:
    cfg = Config.load(provider=provider)
    return RunOptions(cfg.model, cfg.fallbacks, cfg.batch_size, cfg.redact, cfg.redact_ner, cfg.cache_dir is not None, False,
                      cfg.provider, cfg.base_url or "", list(cfg.standards or DEFAULT_STANDARDS), cfg.parallel)


def providers() -> list[dict]:
    Config.load()  # подхватить .env
    return [provider_defaults(p) for p in PROVIDERS]


def standards() -> list[dict]:
    """Все стандарты с критериями. kind: builtin (встроенный), modified (встроенный, изменён), custom (свой)."""
    builtin = criteria_mod.builtin_ids()
    out = []
    for s in sorted(available().values(), key=lambda s: (s.order, s.id)):
        overridden = Path(s.path).parent.resolve() != criteria_mod.BUILTIN_DIR.resolve()
        kind = ("modified" if overridden else "builtin") if s.id in builtin else "custom"
        out.append({"id": s.id, "title": s.title, "cite": s.cite, "description": s.description, "order": s.order,
                    "mandatory": s.id == BASE_STANDARD, "default": s.id in DEFAULT_STANDARDS, "kind": kind,
                    "criteria": [{"code": c["code"], "scope": c.get("scope", "set" if c["code"].startswith("set_") else "requirement"),
                                  "name": c.get("name", ""), "short": c.get("short", ""), "source": c.get("source", ""),
                                  "question": c.get("question", ""), "extends": not c.get("name")}
                                 for c in s.criteria]})
    return out


def standard_raw(sid: str) -> dict:
    try:
        return criteria_mod.raw_standard(sid)
    except criteria_mod.StandardError as e:
        raise NotFound(str(e)) from e


def save_standard(raw: dict, expected_id: str | None = None) -> dict:
    """Сохранить стандарт (свой или изменённый встроенный). StandardError с понятным текстом при ошибке."""
    s = criteria_mod.save_standard(raw, expected_id)
    return next(x for x in standards() if x["id"] == s.id)


def delete_standard(sid: str) -> str:
    if sid not in available():
        raise NotFound(f"Стандарт {sid!r} не найден")
    return criteria_mod.delete_standard(sid)


def draft_standard(text: str, o: RunOptions) -> dict:
    """Черновик стандарта по тексту или описанию: один запрос к модели. Не сохраняется, пока пользователь не нажмёт «Сохранить»."""
    from validator import prompts
    from validator.llm import make_client
    from validator.redact import Redactor
    from validator.schemas import StandardDraft

    cfg = make_config(o)
    red = Redactor(enabled=cfg.redact, use_ner=False)  # текст стандарта обычно публичный, но точные детекторы оставляем
    client = make_client(cfg, red, ROOT / "runs" / "standard_drafts", log=lambda *_: None)
    out = client.generate("standard_draft", prompts.STANDARD_DRAFT_SYSTEM, prompts.standard_draft(red.redact(text)), StandardDraft)
    data = out.model_dump()
    data["id"] = re.sub(r"[^a-z0-9_]+", "_", data["id"].lower()).strip("_")[:40] or "my_standard"
    for c in data["criteria"]:
        c["code"] = re.sub(r"[^a-z0-9_]+", "_", c["code"].lower()).strip("_")[:40]
        if c["scope"] not in criteria_mod.SCOPES:
            c["scope"] = "set" if c["code"].startswith("set_") else "requirement"
        if c["scope"] == "set" and not c["code"].startswith("set_"):
            c["code"] = ("set_" + c["code"])[:40]
        for k in ("name", "short", "source", "question"):
            c[k] = _no_dashes(red.restore(c[k]))
    for k in ("title", "cite", "description"):
        data[k] = _no_dashes(red.restore(data[k]))
    data["order"] = 100
    data["model"] = client.usage.steps[-1][1] if client.usage.steps else cfg.model
    return data


def _no_dashes(s: str) -> str:
    return re.sub(r"\s+[—–]\s+", ": ", s or "").replace("—", ":").replace("–", "-").strip()


def public_options(o: RunOptions) -> dict:
    d = asdict(o)
    d.pop("api_key")
    return d


def has_env_key(provider: str | None = None) -> bool:
    return bool(Config.load(provider=provider).api_key)


def make_config(o: RunOptions) -> Config:
    cfg = Config.load(provider=o.provider, model=o.model, fallbacks=o.fallbacks, batch_size=o.batch_size, redact=o.redact,
                      redact_ner=o.redact_ner, api_key=o.api_key.strip() or None, base_url=o.base_url.strip() or None,
                      standards=o.standards or None, parallel=o.parallel)
    if not o.use_cache:
        cfg.cache_dir = None
    return cfg


# ---------- маскирование (локально, без модели) ----------


def mask_preview(doc_id: str, o: RunOptions) -> dict:
    """Сухая проверка: что уйдёт в модель."""
    res = pipeline.run(str(resolve_document(doc_id)), make_config(o), log=lambda *_: None, dry_run=True)
    masked = (res.audit_dir / "masked_input.md").read_text(encoding="utf-8")
    rows = [{"placeholder": ph, "kind": ph.strip("[]").rsplit("_", 1)[0], "value": _shorten(v)}
            for ph, v in (res.redactor.backward.items() if res.redactor else [])]
    return {"masked": masked, "masked_html": markdown_html(masked, highlight_placeholders=True),
            "provider": o.provider,
            "replacements": rows, "enabled": o.redact}


def _shorten(v: str) -> str:
    """Значение показываем частично: экран могут видеть посторонние."""
    v = v.strip()
    return v if len(v) <= 4 else v[:2] + "…" + v[-2:]


# ---------- проверки ----------


def _runs_dir() -> Path:
    return Path(Config.load().runs_dir)


def run_dir(run_id: str) -> Path:
    d = _runs_dir() / run_id
    if Path(run_id).name != run_id or not (d / "state.pkl").is_file():
        raise NotFound(f"Проверка {run_id!r} не найдена")
    return d


STATUS_RU = {"done": "готова", "running": "прервана", "error": "ошибка", "cancelled": "остановлена"}


def _meta(d: Path) -> dict:
    try:
        return json.loads((d / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def list_runs(include_unfinished: bool = False, active_docs: set[str] | None = None) -> list[dict]:
    """Проверки из runs/, новые первыми. Готовые открываются; незавершённые (ошибка, остановка,
    обрыв сервера) показываются, если include_unfinished, и их можно повторить из кэша."""
    out: list[dict] = []
    root = _runs_dir()
    if not root.is_dir():
        return out
    active_docs = active_docs or set()
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta, rj = _meta(d), d / "result.json"
        done = (d / "state.pkl").is_file() and rj.is_file()
        if meta.get("status") == "preview" or (not done and not meta):
            continue  # предпросмотр маскирования или старая папка без сведений
        if not done and not include_unfinished:
            continue
        item = {"id": d.name, "document": meta.get("document_name", ""), "title": meta.get("title", ""),
                "started": meta.get("started", ""), "status": "done" if done else meta.get("status", "running"),
                "error": meta.get("error", ""), "can_open": done, "can_retry": False,
                "standards": (meta.get("options") or {}).get("standards", []),
                "provider": (meta.get("options") or {}).get("provider", "")}
        if done:
            try:
                r = json.loads(rj.read_text(encoding="utf-8"))
                op = _open_json(r)
                item.update({
                    "document": Path(r["document"]["path"]).name, "title": r["document"].get("title", ""),
                    "started": r.get("started", ""), "requirements": len(r["requirements"]), "open_findings": len(op),
                    "critical": sum(1 for f in op if f["severity"] == "critical"),
                    "development": r.get("decision", {}).get("development", ""), "answered": bool(r.get("answers")),
                    "standards": r.get("standards", item["standards"]), "provider": r.get("provider", item["provider"]),
                    "seconds": r.get("seconds"),
                })
            except (KeyError, ValueError):
                continue
        else:
            if item["status"] == "running" and meta.get("document") in active_docs:
                item["status"] = "active"
            item["can_retry"] = item["status"] != "active" and _allowed_document(meta.get("document"))
        item["status_label"] = {"active": "идёт", **STATUS_RU}.get(item["status"], item["status"])
        out.append(item)
    return out


def _allowed_document(path: str | None) -> bool:
    """Повторять можно только документы из samples/ и загрузок: путь берётся из run.json."""
    if not path:
        return False
    p = Path(path).resolve()
    return p.is_file() and any(base.resolve() in p.parents for base in (SAMPLES, UPLOADS))


def retry_plan(run_id: str, fallback: RunOptions) -> tuple[Path, RunOptions, Path | None]:
    """Документ и настройки незавершённой проверки для повторного запуска."""
    d = _runs_dir() / run_id
    meta = _meta(d) if Path(run_id).name == run_id else {}
    if not meta or not _allowed_document(meta.get("document")):
        raise NotFound(f"Проверку {run_id!r} нельзя повторить: нет сведений о документе")
    o = meta.get("options") or {}
    opts = RunOptions(
        model=o.get("model") or fallback.model, fallbacks=o.get("fallbacks") or [], batch_size=o.get("batch_size") or fallback.batch_size,
        redact=o.get("redact", True), redact_ner=o.get("redact_ner", True), use_cache=True, fix=bool(meta.get("fix")),
        provider=o.get("provider") or fallback.provider, base_url=o.get("base_url") or "",
        standards=o.get("standards") or fallback.standards, parallel=o.get("parallel") or fallback.parallel,
        api_key=fallback.api_key,
    )
    ans = meta.get("answers_path")
    return Path(meta["document"]), opts, Path(ans) if ans and Path(ans).is_file() else None


def load_run(run_id: str):
    return pipeline.load_state(run_dir(run_id))


def run_files(res, pdf: bool = False) -> dict[str, Path]:
    """Отчёт и сопутствующие файлы на диске (runs/web_reports/<проверка>/).

    Markdown пишется заново при каждом обращении (дёшево и всегда в текущем оформлении), PDF только по запросу.
    """
    d = WEB_REPORTS / res.audit_dir.name
    rp = report.write(res, d / f"{res.doc.path.stem}_report.md")
    files = {"report": rp}
    if pdf:
        files["report_pdf"] = report.write(res, rp.with_suffix(".pdf"))
    extras = report.write_extras(res, rp, questions=True)
    for key, label in (("questions", "Вопросы"), ("fixed", "Исправленная")):
        p = next((v for k, v in extras.items() if k.startswith(label)), None)
        if p:
            files[key] = p
    if (res.audit_dir / "result.json").exists():
        files["result"] = res.audit_dir / "result.json"
    return files


def report_markdown(res) -> str:
    return report.Renderer(res).render()


def markdown_html(text: str, highlight_placeholders: bool = False) -> str:
    """Markdown в HTML. Показывать только в iframe с sandbox: в документе может быть сырой HTML."""
    import markdown  # только для страницы отчёта в веб-интерфейсе: терминалу пакет не нужен

    html = markdown.markdown(text, extensions=["tables", "fenced_code", "md_in_html", "sane_lists"])
    if highlight_placeholders:
        html = _PLACEHOLDER.sub(lambda m: f'<mark class="ph">{m.group(0)}</mark>', html)
    return html


def report_html(res) -> str:
    return markdown_html(report_markdown(res))


def run_view(res) -> dict:
    """Всё, что нужно странице результата, одним JSON. Замаскированные значения возвращены на место."""
    r = report.Renderer(res)

    def t(text: str, model: bool = True) -> str:
        """Текст для экрана: значения возвращены на место, разметка Markdown (** и `) убрана."""
        return re.sub(r"\*\*|`", "", r.t(text, model))

    op = res.open_findings()
    sev = Counter(f.severity for f in op)
    per_req = Counter(rid for f in op if f.scope == "requirement" for rid in f.req_ids)
    u = res.usage
    checks = getattr(res, "acceptance_checks", [])
    coverage_pairs = {(c.req_id, c.acceptance_id): c.status for c in checks if c.req_id}
    coverage_rows = {c.req_id: c for c in res.coverage}
    coverage = []
    for q in res.requirements:
        if q.kind != "functional":
            continue
        row = coverage_rows.get(q.rid)
        ids = list(dict.fromkeys([*(row.acceptance_ids if row else []),
                                   *(aid for rid, aid in coverage_pairs if rid == q.rid)]))
        coverage.append({"req_id": q.rid, "acceptance_ids": ids,
                         "acceptance_statuses": {aid: coverage_pairs.get((q.rid, aid), "unreviewed") for aid in ids},
                         "reviewed": row is not None,
                         "missing_scenarios": [t(s) for s in row.missing_scenarios] if row else []})
    anchor = lambda a: {"text": t(a.text, model=False), "where": a.where(), "line": a.line, "line_end": a.line_end,  # noqa: E731
                        "page": a.page, "section": a.section}
    return {
        "id": res.audit_dir.name,
        "document": {"name": res.doc.path.name, "title": t(res.doc.title, model=False), "format": res.doc.fmt,
                     "sha256": res.doc.sha256, "lines": len(res.doc.lines)},
        "started": res.started.isoformat(),
        "agent_version": res.agent_version,
        "summary": {"requirements": len(res.requirements), "open": len(op), "closed": len(res.kept()) - len(op),
                    "critical": sev["critical"], "major": sev["major"], "minor": sev["minor"],
                    "questions": len(res.questions),
                    "rejected": sum(not f.kept and f.verification_status != "unreviewed" for f in res.findings),
                    "unreviewed_findings": sum(f.verification_status == "unreviewed" for f in res.findings)},
        "decision": {"development": res.decision_dev, "testing": res.decision_test,
                     "development_rationale": t(res.conclusion.development_rationale) if res.conclusion else "",
                     "testing_rationale": t(res.conclusion.testing_rationale) if res.conclusion else "",
                     "overall": t(res.conclusion.overall) if res.conclusion else "",
                     "fix_first": [{"title": t(x.title), "finding_ids": x.finding_ids, "action": t(x.action)}
                                   for x in (res.conclusion.fix_first if res.conclusion else [])]},
        "requirements": [{"id": q.rid, "doc_id": q.doc_id, "section": q.section, "kind": q.kind, "summary": t(q.summary),
                          "quote": t(q.quote, model=False), "where": q.anchor.where(), "open_findings": per_req.get(q.rid, 0),
                          "verdicts": {k: {"verdict": v, "comment": t(c)} for k, (v, c) in res.req_verdicts.get(q.rid, {}).items()}}
                         for q in res.requirements],
        "findings": [{"id": f.fid, "scope": f.scope, "criterion": f.criterion,
                      "criterion_name": res.prof.name(f.criterion),
                      "severity": f.severity, "req_ids": f.req_ids, "anchors": [anchor(a) for a in f.anchors],
                      "problem": t(f.problem), "fix": t(f.fix), "source": f.source,
                      "status": f.resolution or "open", "resolution_note": t(f.resolution_note)}
                     for f in res.kept()],
        "set_verdicts": [{"criterion": k, "criterion_name": res.prof.name(k), "verdict": v, "comment": t(c)}
                         for k, (v, c) in res.set_verdicts.items()],
        "questions": [{"id": f"В-{i}", "priority": q.priority, "priority_label": PRIORITY_LABEL.get(q.priority, q.priority),
                       "addressee": q.addressee, "question": t(q.question), "why": t(q.why),
                       "quote": t(q.quote, model=False), "options": [t(o) for o in q.options],
                       "req_ids": q.req_ids, "finding_ids": q.finding_ids}
                      for i, q in enumerate(res.questions, 1)],
        "answers": [{"id": a.qid, "answer": a.answer} for a in res.answers],
        "checklist": [{"item": t(c.item, model=False), "answer": c.answer, "rationale": t(c.rationale),
                       "finding_ids": c.finding_ids} for c in res.checklist],
        "coverage": coverage,
        "acceptance_checks": [{**c.model_dump(), "expected": t(c.expected), "stated": t(c.stated),
                               "question": t(c.question)} for c in checks],
        "standards": [{"id": s.id, "title": s.title} for s in res.prof.standards],
        "criteria": [{"code": c.code, "name": c.name, "column": c.column, "source": c.source}
                     for c in res.prof.requirement],
        "elapsed": round((res.finished - res.started).total_seconds()) if res.finished else None,
        "passport": {"model": res.config.model, "provider": getattr(res.config, "provider", "gemini"), "calls": getattr(u, "calls", 0), "cached": getattr(u, "cached", 0),
                     "steps": [{"step": s, "model": m} for s, m in (u.steps if u else [])],
                     "masking": res.redactor.stats() if res.redactor else {}, "warnings": res.warnings,
                     "phase_seconds": getattr(res, "phase_seconds", {})},
        "has_fixed": res.fixed is not None,
    }


# ---------- ответы заказчика ----------


def write_answers(res, answers: dict[str, str]) -> Path:
    """Файл вопросов в формате clarify.parse_answers с ответами из интерфейса (ключи «В-1», «В-2»…)."""
    p = write_questions(res, WEB_REPORTS / res.audit_dir.name / f"{res.doc.path.stem}_answers.md")
    lines, qid = p.read_text(encoding="utf-8").splitlines(), None
    for i, ln in enumerate(lines):
        m = re.match(r"^##\s+(В-\d+)\.", ln)
        if m:
            qid = m.group(1)
        elif ln.startswith("Ответ:") and qid and (answers.get(qid) or "").strip():
            lines[i] = "Ответ: " + " ".join(answers[qid].split())
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------- сравнение двух проверок ----------


def _open_json(r: dict) -> list[dict]:
    return [f for f in r["findings"] if f["kept"] and not f["duplicate_of"] and f.get("resolution") != "closed"]


def compare(run_a: str, run_b: str) -> dict:
    """Те же цифры, что eval/compare.py."""
    def stats(run_id):
        r = json.loads((run_dir(run_id) / "result.json").read_text(encoding="utf-8"))
        op = _open_json(r)
        return {"run": run_id, "document": Path(r["document"]["path"]).name, "requirements": len(r["requirements"]),
                "open": len(op), **{k: sum(1 for f in op if f["severity"] == k) for k in SEV_LABEL},
                "questions": len(r.get("questions", [])), "development": r["decision"]["development"],
                "by_criterion": Counter(f["criterion"] for f in op)}

    a, b = stats(run_a), stats(run_b)
    crit = [{"criterion": c, "name": criterion_name(c), "before": a["by_criterion"][c], "after": b["by_criterion"][c]}
            for c in sorted(set(a["by_criterion"]) | set(b["by_criterion"]), key=lambda c: -a["by_criterion"][c])]
    for s in (a, b):
        s.pop("by_criterion")
    return {"before": a, "after": b, "by_criterion": crit}


# ---------- рабочая редакция документа ----------
# Копия текста документа в runs/<проверка>/draft/, которую правят помощник и пользователь.
# Каждая правка пишется в журнал с предыдущим текстом, поэтому её можно отменить.
# Исходный файл документа не меняется; редакцию можно скачать или отправить на новую проверку.

MAX_HISTORY = 50


def _draft_dir(res) -> Path:
    return res.audit_dir / "draft"


def _draft_log(res) -> list[dict]:
    try:
        return json.loads((_draft_dir(res) / "history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def draft_text(res) -> str:
    p = _draft_dir(res) / "draft.md"
    return p.read_text(encoding="utf-8") if p.is_file() else res.doc.text


def _save_draft(res, text: str, entry: dict) -> None:
    d = _draft_dir(res)
    d.mkdir(parents=True, exist_ok=True)
    log = _draft_log(res)
    log.append({**entry, "prev": draft_text(res), "at": __import__("datetime").datetime.now().isoformat(timespec="seconds")})
    (d / "history.json").write_text(json.dumps(log[-MAX_HISTORY:], ensure_ascii=False, indent=1), encoding="utf-8")
    tmp = d / "draft.tmp"
    tmp.write_text(text, encoding="utf-8")
    __import__("os").replace(tmp, d / "draft.md")


def _stem(res) -> str:
    """Имя документа без префикса хэша загрузки (1e088fbcbbd8_имя.md)."""
    return re.sub(r"^[0-9a-f]{12}_", "", res.doc.path.stem)


def draft_view(res) -> dict:
    text = draft_text(res)
    log = _draft_log(res)
    return {"text": text, "changed": text != res.doc.text, "lines": len(text.split("\n")),
            "edits": [{k: e.get(k, "") for k in ("kind", "reason", "old", "new", "at")} for e in log],
            "can_undo": bool(log), "name": f"{_stem(res)}_редакция.md"}


class EditError(ValueError):
    pass


_NUM_PREFIX = re.compile(r"(?m)^\s*\d+\|\s?")


def _locate(text: str, old: str, line: int) -> tuple[int, int]:
    """Где в тексте фрагмент old: точное совпадение, а если пробелы разошлись, совпадение по словам.
    Из нескольких совпадений берём ближайшее к строке line."""
    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    spans = [(m.start(), m.end()) for m in re.finditer(re.escape(old), text)]
    if not spans:
        words = old.split()
        if words:
            pat = r"\s+".join(re.escape(w) for w in words)
            spans = [(m.start(), m.end()) for m in re.finditer(pat, text)]
    if not spans:
        raise EditError("Фрагмент не найден в редакции: возможно, его уже изменили. Попросите помощника предложить правку заново.")
    return min(spans, key=lambda s: abs(line_of(s[0]) - (line or 1)))


def apply_edit(res, old_text: str, new_text: str, line: int = 0, reason: str = "") -> dict:
    text = draft_text(res)
    old = _NUM_PREFIX.sub("", old_text or "").strip("\n")
    new = _NUM_PREFIX.sub("", new_text or "")
    if old.strip():
        a, b = _locate(text, old, line)
        updated = text[:a] + new + text[b:]
    else:  # вставка после строки line
        lines = text.split("\n")
        at = max(0, min(int(line or len(lines)), len(lines)))
        updated = "\n".join(lines[:at] + new.split("\n") + lines[at:])
    if updated == text:
        raise EditError("Правка ничего не меняет")
    _save_draft(res, updated, {"kind": "edit", "reason": reason, "old": old, "new": new})
    return draft_view(res)


def set_draft(res, text: str) -> dict:
    text = text.replace("\r\n", "\n")
    if text != draft_text(res):
        _save_draft(res, text, {"kind": "manual", "reason": "правка вручную"})
    return draft_view(res)


def undo_draft(res) -> dict:
    log = _draft_log(res)
    if not log:
        raise EditError("Отменять нечего")
    last = log.pop()
    d = _draft_dir(res)
    (d / "draft.md").write_text(last["prev"], encoding="utf-8")
    (d / "history.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    return draft_view(res)


def reset_draft(res) -> dict:
    if draft_text(res) != res.doc.text:
        _save_draft(res, res.doc.text, {"kind": "reset", "reason": "возврат к исходному тексту"})
    return draft_view(res)


def draft_upload(res) -> str:
    """Редакция как загруженный документ: её можно проверить заново и сравнить «до и после»."""
    return save_upload(f"{_stem(res)}_редакция.md", draft_text(res).encode("utf-8"))


# ---------- ИИ-помощник ----------


def assistant(res, question: str, history: list[dict], focus: dict | None, o: RunOptions) -> dict:
    """Ответ помощника по результатам проверки.

    Всё, что уходит в модель, маскируется той же таблицей замен, что и при проверке: вопрос
    пользователя, прошлые реплики и контекст. Перед отправкой срабатывает тот же Redactor.guard().
    """
    import re as _re

    from validator import prompts
    from validator.llm import make_client
    from validator.pipeline import _findings_block, _numbered
    from validator.schemas import AssistantReply

    red = res.redactor
    mask = red.redact if red else (lambda s: s)
    mask_q = red.redact_query if red else (lambda s: s)
    unmask = red.restore if red else (lambda s: s)
    draft = draft_text(res)
    # помощник видит рабочую редакцию, если её уже правили: иначе его правки не найдутся в тексте
    masked_doc = mask(draft) if draft != res.doc.text else (res.audit_dir / "masked_input.md").read_text(encoding="utf-8")
    questions = "\n".join(f"В-{i}. {q.question} Варианты: " + "; ".join(f"Вариант {k}: {o}" for k, o in enumerate(q.options, 1))
                          for i, q in enumerate(res.questions, 1))

    op = res.open_findings()
    summary = (f"Разработка: {res.decision_dev}. Тестирование: {res.decision_test}.\n"
               f"Требований {len(res.requirements)}, открытых нарушений {len(op)}.\n" + _findings_block(op, brief=True))

    focus_txt = ""
    if focus and focus.get("id"):
        fid = focus["id"]
        if focus.get("type") == "finding":
            f = next((x for x in res.findings if x.fid == fid), None)
            focus_txt = _findings_block([f]) if f else ""
        elif focus.get("type") == "requirement":
            q = res.req(fid)
            if q:
                own = [f for f in op if fid in f.req_ids]
                focus_txt = (f"{q.rid} (в документе {q.doc_id}), строки {q.anchor.line}-{q.anchor.line_end}: «{q.quote}»\n"
                             + (_findings_block(own) if own else "Нарушений по нему нет."))
        elif focus.get("type") == "question":
            i = int(_re.sub(r"\D", "", fid) or 0) - 1
            if 0 <= i < len(res.questions):
                qq = res.questions[i]
                focus_txt = f"{fid}. {qq.question}\nПочему важно: {qq.why}\nВарианты: " + "; ".join(qq.options)
        focus_txt = mask(focus_txt)

    dialog = "\n".join(
        ("Пользователь: " if h.get("role") == "user" else "Помощник: ") + mask_q(str(h.get("content", ""))[:1500])
        for h in history[-6:]
    )
    cfg = make_config(o)
    client = make_client(cfg, red, res.audit_dir / "assistant", log=lambda *_: None)
    out = client.generate("assistant", prompts.ASSISTANT_SYSTEM,
                          prompts.assistant(_numbered(masked_doc), summary, focus_txt, dialog, mask_q(question),
                                            mask(questions)),
                          AssistantReply)
    clean = lambda s: _re.sub(r"\s+[—–]\s+", ": ", unmask(s or "")).strip()  # noqa: E731
    model = client.usage.steps[-1][1] if client.usage.steps else cfg.model
    known_q = {f"В-{i}" for i in range(1, len(res.questions) + 1)}
    edits = [{"line": e.line, "old_text": unmask(_NUM_PREFIX.sub("", e.old_text)).strip("\n"),
              "new_text": unmask(_NUM_PREFIX.sub("", e.new_text)), "reason": clean(e.reason)}
             for e in out.edits if (e.new_text or "").strip()]
    for e in edits:  # заранее проверяем, что фрагмент есть в редакции: кнопку «Применить» показываем только для таких
        try:
            if e["old_text"].strip():
                _locate(draft, e["old_text"], e["line"])
            e["ok"] = True
        except EditError:
            e["ok"] = False
    answers = [{"question_id": a.question_id.replace("B", "В").strip(), "answer": clean(a.answer), "basis": clean(a.basis)}
               for a in out.answers if a.answer.strip()]
    answers = [a for a in answers if a["question_id"] in known_q]
    return {"answer": clean(out.answer), "suggested_text": clean(out.suggested_text), "model": model,
            "edits": edits, "answers": answers}
