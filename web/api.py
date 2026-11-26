"""HTTP API агента и раздача фронтенда.

    python -m web            # http://127.0.0.1:8000

Документация API со всеми схемами: http://127.0.0.1:8000/docs
Ключ Gemini берётся из .env; без него клиент может передать свой в заголовке
X-Gemini-Key (только для запуска прогона, на диск не пишется).
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
from .jobs import Busy, JobRegistry

STATIC = Path(__file__).resolve().parent / "static"
mimetypes.add_type("font/woff2", ".woff2")  # в реестре Windows его может не быть
MAX_UPLOAD = 20 * 1024 * 1024

app = FastAPI(title="Агент проверки требований", version=__version__,
              description="Статическая проверка требований по ISO/IEC/IEEE 29148 и IEEE 830")
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
    """Настройки прогона; не переданные поля берутся из .env."""
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


class AnswersRequest(BaseModel):
    answers: dict[str, str] = Field(description="{'В-1': 'текст или Вариант N: …'}")
    options: Options = Options(fix=True)


def _options(o: Options, api_key: str | None = None) -> services.RunOptions:
    base = services.default_options()
    for k, v in o.model_dump(exclude_none=True).items():
        setattr(base, k, v)
    base.api_key = (api_key or "").strip()
    return base


def _need_key(api_key: str | None) -> None:
    if not (api_key or "").strip() and not services.has_env_key():
        raise HTTPException(400, "Нужен ключ Gemini API: впишите GEMINI_API_KEY в .env или передайте заголовок X-Gemini-Key")


def _start(path, opts, answers_path=None) -> dict:
    try:
        return REGISTRY.start(path, opts, answers_path).public()
    except Busy as e:
        raise HTTPException(409, str(e)) from e


def _run(run_id: str):
    try:
        return services.load_run(run_id)
    except services.NotFound as e:
        raise HTTPException(404, str(e)) from e


# ---------- служебное ----------


@app.get("/api/status")
def status():
    running = REGISTRY.running()
    return {"version": __version__, "has_env_key": services.has_env_key(),
            "defaults": services.public_options(services.default_options()),
            "formats": services.FORMATS, "running_job": running[0].jid if running else None}


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


# ---------- прогоны ----------


@app.post("/api/jobs", status_code=202)
def start_job(req: JobRequest, x_gemini_key: str | None = Header(None)):
    _need_key(x_gemini_key)
    try:
        path = services.resolve_document(req.document)
    except services.NotFound as e:
        raise HTTPException(404, str(e)) from e
    return _start(path, _options(req.options, x_gemini_key))


@app.get("/api/jobs/{job_id}")
def job(job_id: str, log_from: int = Query(0, ge=0, description="отдать журнал начиная с этой строки")):
    j = REGISTRY.get(job_id)
    if not j:
        raise HTTPException(404, "Задача не найдена")
    return j.public(log_from)


@app.get("/api/runs")
def runs():
    return services.list_runs()


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
        raise HTTPException(404, "Такого файла у прогона нет")
    return FileResponse(files[kind], filename=files[kind].name)


@app.post("/api/runs/{run_id}/answers", status_code=202)
def answer(run_id: str, req: AnswersRequest, x_gemini_key: str | None = Header(None)):
    """Ответы заказчика: повторный прогон закроет устранённые дефекты и пересчитает готовность."""
    _need_key(x_gemini_key)
    res = _run(run_id)
    if not any(v.strip() for v in req.answers.values()):
        raise HTTPException(400, "Ни одного ответа не заполнено")
    path = services.write_answers(res, req.answers)
    return _start(res.doc.path, _options(req.options, x_gemini_key), path)


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
