"""HTTP API агента и раздача фронтенда.

    python -m web            # http://127.0.0.1:8000

Документация API со всеми схемами: http://127.0.0.1:8000/docs
Ключ провайдера (Gemini, OpenAI-совместимый, Anthropic) берётся из .env; без него
клиент может передать свой в заголовке X-Api-Key (только для запуска проверки,
на диск не пишется). Несколько проверок идут одновременно, остальные ждут в очереди.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from validator import __version__

from . import services
from .jobs import JobRegistry

STATIC = Path(__file__).resolve().parent / "static"
mimetypes.add_type("font/woff2", ".woff2")  # в реестре Windows его может не быть
MAX_UPLOAD = 20 * 1024 * 1024

app = FastAPI(title="Агент проверки требований", version=__version__,
              description="Статическая проверка требований по ISO/IEC/IEEE 29148, IEEE 830 и другим стандартам")
REGISTRY = JobRegistry()


@app.middleware("http")
async def fonts_cors(request, call_next):
    """Отчёт показывается в iframe с sandbox (чужой origin), а шрифты браузер грузит туда только с CORS."""
    response = await call_next(request)
    if request.url.path.startswith("/static/fonts/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ---------- схемы запросов ----------


class Options(BaseModel):
    """Настройки проверки; не переданные поля берутся из .env."""
    provider: str | None = Field(None, pattern="^(gemini|openai|anthropic)$")
    base_url: str | None = Field(None, description="Только для OpenAI-совместимых серверов")
    standards: list[str] | None = Field(None, description="ID стандартов (/api/standards); ISO 29148 включён всегда")
    parallel: int | None = Field(None, ge=1, le=16)
    model: str | None = None
    fallbacks: list[str] | None = None
    batch_size: int | None = Field(None, ge=1, le=40)
    redact: bool | None = None
    redact_ner: bool | None = None
    use_cache: bool | None = None
    fix: bool | None = None


class PreviewRequest(BaseModel):
    document: str = Field(description="sample:<имя> или upload:<имя>")
    options: Options = Options()


class JobRequest(BaseModel):
    document: str
    options: Options = Options()


class AssistantRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[dict] = Field(default_factory=list, description="[{'role': 'user'|'assistant', 'content': '...'}]")
    focus: dict | None = Field(None, description="{'type': 'finding'|'requirement'|'question', 'id': 'Н-03'}")
    options: Options = Options()


class AnswersRequest(BaseModel):
    answers: dict[str, str] = Field(description="{'В-1': 'текст или Вариант N: …'}")
    options: Options = Options(fix=True)


def _options(o: Options, api_key: str | None = None) -> services.RunOptions:
    base = services.default_options(o.provider)
    for k, v in o.model_dump(exclude_none=True).items():
        setattr(base, k, v)
    base.api_key = (api_key or "").strip()
    if base.standards:
        from validator.criteria import StandardError, profile

        try:
            profile(base.standards)
        except StandardError as e:
            raise HTTPException(400, str(e)) from e
    return base


def _need_key(opts: services.RunOptions) -> None:
    if opts.api_key or services.has_env_key(opts.provider) or (opts.provider == "openai" and opts.base_url):
        return
    env = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[opts.provider]
    raise HTTPException(400, f"Нужен ключ API: впишите {env} в .env или укажите ключ в настройках")


def _key(x_api_key: str | None, x_gemini_key: str | None) -> str | None:
    return x_api_key or x_gemini_key  # X-Gemini-Key оставлен для совместимости


def _start(path, opts, answers_path=None) -> dict:
    return REGISTRY.start(path, opts, answers_path).public()


def _run(run_id: str):
    try:
        return services.load_run(run_id)
    except services.NotFound as e:
        raise HTTPException(404, str(e)) from e


# ---------- служебное ----------


@app.get("/api/status")
def status():
    d = services.default_options()
    return {"version": __version__, "has_env_key": services.has_env_key(d.provider),
            "defaults": services.public_options(d), "providers": services.providers(),
            "standards": services.standards(), "formats": services.FORMATS,
            "max_parallel": REGISTRY.max_parallel,
            "active_jobs": [j.jid for j in REGISTRY.running()]}


@app.get("/api/standards")
def standards():
    return services.standards()


# ---------- документы ----------


@app.get("/api/samples")
def samples():
    return services.samples()


@app.post("/api/documents")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Файл больше 20 МБ")
    try:
        doc_id = services.save_upload(file.filename or "document", data)
        return services.document_info(doc_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/documents/{doc_id:path}")
def document(doc_id: str):
    try:
        return services.document_info(doc_id)
    except services.NotFound as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:  # скан без текстового слоя и т.п.
        raise HTTPException(400, str(e)) from e


@app.post("/api/preview")
def preview(req: PreviewRequest):
    """Что уйдёт в модель: маскированный текст и таблица замен. Локально, без запросов к модели."""
    try:
        return services.mask_preview(req.document, _options(req.options))
    except services.NotFound as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---------- проверки ----------


@app.post("/api/jobs", status_code=202)
def start_job(req: JobRequest, x_api_key: str | None = Header(None), x_gemini_key: str | None = Header(None)):
    """Поставить проверку в очередь. Для нескольких документов вызовите несколько раз: они пойдут параллельно."""
    opts = _options(req.options, _key(x_api_key, x_gemini_key))
    _need_key(opts)
    try:
        path = services.resolve_document(req.document)
    except services.NotFound as e:
        raise HTTPException(404, str(e)) from e
    return _start(path, opts)


@app.get("/api/jobs")
def jobs():
    """Все проверки этого запуска сервера (без журналов), новые первыми."""
    return [j.public(with_log=False) for j in REGISTRY.all()]


@app.get("/api/jobs/{job_id}")
def job(job_id: str, log_from: int = Query(0, ge=0, description="отдать журнал начиная с этой строки")):
    j = REGISTRY.get(job_id)
    if not j:
        raise HTTPException(404, "Задача не найдена")
    return j.public(log_from)


@app.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, x_api_key: str | None = Header(None), x_gemini_key: str | None = Header(None)):
    """Повторить проверку из списка (после ошибки или остановки)."""
    old = REGISTRY.get(job_id)
    if not old:
        raise HTTPException(404, "Задача не найдена")
    if old.active:
        raise HTTPException(409, "Проверка ещё идёт")
    key = (_key(x_api_key, x_gemini_key) or "").strip()
    import dataclasses

    _need_key(dataclasses.replace(old.options, api_key=key))
    return REGISTRY.retry(job_id, key).public()


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    """Остановить проверку. Готовые шаги остаются в кэше, повторный запуск продолжит с места остановки."""
    j = REGISTRY.stop(job_id)
    if not j:
        raise HTTPException(404, "Задача не найдена")
    return j.public(with_log=False)


@app.get("/api/runs")
def runs(all: bool = Query(False, description="включить незавершённые проверки (ошибка, остановка, обрыв)")):
    active = {str(j.input_path.resolve()) for j in REGISTRY.running()}
    return services.list_runs(include_unfinished=all, active_docs=active)


@app.post("/api/runs/{run_id}/retry", status_code=202)
def retry_run(run_id: str, x_api_key: str | None = Header(None), x_gemini_key: str | None = Header(None)):
    """Повторить незавершённую проверку из истории с теми же настройками (готовые шаги из кэша)."""
    try:
        doc, opts, answers = services.retry_plan(run_id, services.default_options())
    except services.NotFound as e:
        raise HTTPException(404, str(e)) from e
    opts.api_key = (_key(x_api_key, x_gemini_key) or "").strip()
    _need_key(opts)
    return _start(doc, opts, answers)


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    return services.run_view(_run(run_id))


@app.get("/api/runs/{run_id}/report", response_class=HTMLResponse)
def run_report(run_id: str, format: str = Query("html", pattern="^(html|md)$")):
    res = _run(run_id)
    if format == "md":
        return PlainTextResponse(services.report_markdown(res), media_type="text/markdown; charset=utf-8")
    return HTMLResponse(services.report_html(res))


@app.get("/api/runs/{run_id}/files/{kind}")
def run_file(run_id: str, kind: str):
    """kind: report | questions | fixed | result"""
    files = services.run_files(_run(run_id))
    if kind not in files:
        raise HTTPException(404, "Такого файла у проверки нет")
    return FileResponse(files[kind], filename=files[kind].name)


@app.post("/api/runs/{run_id}/answers", status_code=202)
def answer(run_id: str, req: AnswersRequest, x_api_key: str | None = Header(None), x_gemini_key: str | None = Header(None)):
    """Ответы заказчика: повторная проверка закроет устранённые дефекты и пересчитает готовность."""
    res = _run(run_id)
    if not req.options.standards:  # повторная проверка по тем же стандартам, что и исходная
        req.options.standards = res.prof.ids
    opts = _options(req.options, _key(x_api_key, x_gemini_key))
    _need_key(opts)
    if not any(v.strip() for v in req.answers.values()):
        raise HTTPException(400, "Ни одного ответа не заполнено")
    path = services.write_answers(res, req.answers)
    return _start(res.doc.path, opts, path)


@app.post("/api/runs/{run_id}/assistant")
def run_assistant(run_id: str, req: AssistantRequest, x_api_key: str | None = Header(None),
                  x_gemini_key: str | None = Header(None)):
    """ИИ-помощник: объяснить нарушение, разбить требование, предложить формулировку или тест-кейсы."""
    from validator.llm import LLMError
    from validator.redact import RedactionLeak

    res = _run(run_id)
    opts = _options(req.options, _key(x_api_key, x_gemini_key))
    _need_key(opts)
    try:
        return services.assistant(res, req.question, req.history, req.focus, opts)
    except (LLMError, RedactionLeak) as e:
        raise HTTPException(502, f"Помощник не ответил: {e}") from e


@app.get("/api/compare")
def compare(before: str, after: str):
    try:
        return services.compare(before, after)
    except services.NotFound as e:
        raise HTTPException(404, str(e)) from e


# ---------- фронтенд ----------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
