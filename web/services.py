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

import markdown

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
    return [{"id": s.id, "title": s.title, "description": s.description, "mandatory": s.id == BASE_STANDARD,
             "default": s.id in DEFAULT_STANDARDS,
             "criteria": [c.get("name") for c in s.criteria if c.get("name")]}
            for s in sorted(available().values(), key=lambda s: s.order)]


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


def list_runs() -> list[dict]:
    out: list[dict] = []
    root = _runs_dir()
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        rj = d / "result.json"
        if not d.is_dir() or not (d / "state.pkl").is_file() or not rj.is_file():
            continue
        try:
            r = json.loads(rj.read_text(encoding="utf-8"))
            op = _open_json(r)
            out.append({
                "id": d.name, "document": Path(r["document"]["path"]).name, "title": r["document"].get("title", ""),
                "started": r.get("started", ""), "requirements": len(r["requirements"]), "open_findings": len(op),
                "critical": sum(1 for f in op if f["severity"] == "critical"),
                "development": r.get("decision", {}).get("development", ""), "answered": bool(r.get("answers")),
                "standards": r.get("standards", []), "provider": r.get("provider", ""),
            })
        except (KeyError, ValueError):
            continue
    return out


def load_run(run_id: str):
    return pipeline.load_state(run_dir(run_id))


def run_files(res) -> dict[str, Path]:
    """Отчёт и сопутствующие файлы на диске (runs/web_reports/<проверка>/), создаются при первом обращении."""
    d = WEB_REPORTS / res.audit_dir.name
    rp = d / f"{res.doc.path.stem}_report.md"
    if not rp.exists():
        report.write(res, rp)
    files = {"report": rp}
    extras = report.write_extras(res, rp)
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
                    "questions": len(res.questions), "rejected": sum(1 for f in res.findings if not f.kept)},
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
        "coverage": [{"req_id": c.req_id, "acceptance_ids": c.acceptance_ids, "missing_scenarios": [t(s) for s in c.missing_scenarios]}
                     for c in res.coverage],
        "standards": [{"id": s.id, "title": s.title} for s in res.prof.standards],
        "criteria": [{"code": c.code, "name": c.name, "column": c.column, "source": c.source}
                     for c in res.prof.requirement],
        "elapsed": round((res.finished - res.started).total_seconds()) if res.finished else None,
        "passport": {"model": res.config.model, "provider": getattr(res.config, "provider", "gemini"), "calls": getattr(u, "calls", 0), "cached": getattr(u, "cached", 0),
                     "steps": [{"step": s, "model": m} for s, m in (u.steps if u else [])],
                     "masking": res.redactor.stats() if res.redactor else {}, "warnings": res.warnings},
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
    masked_doc = (res.audit_dir / "masked_input.md").read_text(encoding="utf-8")

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
                          prompts.assistant(_numbered(masked_doc), summary, focus_txt, dialog, mask_q(question)),
                          AssistantReply)
    clean = lambda s: _re.sub(r"\s+[—–]\s+", ": ", unmask(s or "")).strip()  # noqa: E731
    model = client.usage.steps[-1][1] if client.usage.steps else cfg.model
    return {"answer": clean(out.answer), "suggested_text": clean(out.suggested_text), "model": model}
