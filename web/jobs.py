"""Фоновые проверки: очередь, параллельный запуск нескольких документов, остановка.

Полная проверка идёт минуты, поэтому HTTP-запрос только ставит задачу, а
проверка живёт в отдельном потоке. Одновременно идут до VALIDATOR_MAX_JOBS
проверок (по умолчанию 3), остальные ждут в очереди. Остановка срабатывает
между шагами и во время пауз; ответы, полученные до остановки, остаются в
кэше, и повторный запуск продолжит с того же места.

Если проверка упала из-за модели (перегрузка, лимит, обрыв), она сама
перезапускается до VALIDATOR_AUTO_RETRY раз (по умолчанию 2) после паузы;
готовые шаги берутся из кэша, поэтому повтор стоит только недостающих запросов.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from validator import pipeline
from validator.llm import Cancelled, LLMError
from validator.redact import RedactionLeak

from .services import RunOptions, make_config


# Шаги конвейера (по строкам «▸ …» журнала) и доля готовности к началу шага: по замерам
# дольше всего идут проверка требований и верификация находок.
STAGES = [
    ("Нарезка", 0.05, "Нарезка на требования"),
    ("Детерминированные", 0.2, "Правила без модели"),
    ("Проверка требований", 0.25, "Проверка требований"),
    ("Верификация", 0.6, "Верификация находок"),
    ("Объединение дублей", 0.75, "Объединение дублей"),
    ("Учёт ответов", 0.8, "Учёт ответов заказчика"),
    ("Вопросы для уточнения", 0.85, "Вопросы и итог"),
]


@dataclass
class Job:
    jid: str
    input_path: Path
    options: RunOptions
    answers_path: Path | None = None
    status: str = "queued"  # queued | running | done | error | cancelled
    log: list[str] = field(default_factory=list)
    run_id: str = ""  # папка проверки в runs/, когда готово
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    stop_requested: bool = False
    attempt: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def write(self, *parts) -> None:
        with self._lock:
            self.log.append(" ".join(str(p) for p in parts))

    def log_since(self, offset: int = 0) -> list[str]:
        with self._lock:
            return self.log[offset:]

    @property
    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        return (self.finished or time.time()) - self.started

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def progress(self) -> tuple[float, str]:
        """Доля готовности по последнему начатому шагу конвейера и название шага (для строки состояния)."""
        if self.status == "done":
            return 1.0, "Готово"
        if self.status == "queued":
            return 0.0, "В очереди"
        frac, stage = 0.02, "Чтение документа"
        for ln in self.log_since(0):
            if ln.startswith("Проверка не удалась"):
                frac, stage = 0.02, "Перезапуск"
            if not ln.startswith("▸"):
                continue
            for key, f, label in STAGES:
                if key in ln:
                    frac, stage = f, label
        return frac, stage

    def public(self, log_offset: int = 0, with_log: bool = True) -> dict:
        lines = self.log_since(log_offset) if with_log else []
        last = self.log_since(max(0, len(self.log) - 1))
        frac, stage = self.progress()
        return {"id": self.jid, "status": self.status, "document": self.input_path.name,
                "progress": round(frac, 2), "stage": stage,
                "with_answers": self.answers_path is not None, "fix": self.options.fix,
                "provider": self.options.provider, "model": self.options.model,
                "elapsed": round(self.elapsed, 1), "run_id": self.run_id, "error": self.error,
                "stop_requested": self.stop_requested, "last": last[0] if last else "", "attempt": self.attempt,
                "log": lines, "log_next": log_offset + len(lines)}


class JobRegistry:
    def __init__(self, client_factory=None, max_parallel: int | None = None):
        self.jobs: dict[str, Job] = {}
        self.client_factory = client_factory  # для тестов: поддельная модель
        self.max_parallel = max_parallel or int(os.getenv("VALIDATOR_MAX_JOBS", "3"))
        self.auto_retry = int(os.getenv("VALIDATOR_AUTO_RETRY", "2"))
        self.retry_pause = float(os.getenv("VALIDATOR_RETRY_PAUSE", "45"))
        self._slots = threading.Semaphore(self.max_parallel)
        self._lock = threading.Lock()

    def start(self, input_path: str | Path, options: RunOptions, answers_path: str | Path | None = None) -> Job:
        job = Job(uuid.uuid4().hex[:8], Path(input_path), options, Path(answers_path) if answers_path else None)
        with self._lock:
            self.jobs[job.jid] = job
        job.write(f"В очереди: {job.input_path.name}")
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"validator-{job.jid}").start()
        return job

    def get(self, jid: str) -> Job | None:
        return self.jobs.get(jid)

    def all(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.active]

    def retry(self, jid: str, api_key: str = "") -> Job | None:
        """Повторить завершившуюся проверку с теми же настройками; готовые шаги придут из кэша."""
        old = self.jobs.get(jid)
        if not old or old.active:
            return None
        import dataclasses

        opts = dataclasses.replace(old.options, api_key=api_key, use_cache=True)
        return self.start(old.input_path, opts, old.answers_path)

    def stop(self, jid: str) -> Job | None:
        job = self.jobs.get(jid)
        if job and job.active:
            job.stop_requested = True
            job.write("Остановка по запросу пользователя…")
        return job

    def _run(self, job: Job) -> None:
        acquired = False
        try:
            while not self._slots.acquire(timeout=0.5):  # ждём свободного места в очереди
                if job.stop_requested:
                    raise Cancelled("Проверка остановлена до начала")
            acquired = True
            if job.stop_requested:
                raise Cancelled("Проверка остановлена до начала")
            job.status, job.started = "running", time.time()
            while True:
                try:
                    res = pipeline.run(
                        str(job.input_path), make_config(job.options), log=job.write,
                        answers_path=str(job.answers_path) if job.answers_path else None, fix=job.options.fix,
                        client_factory=self.client_factory, should_stop=lambda: job.stop_requested,
                    )
                    break
                except (LLMError, RuntimeError) as e:
                    # сбой модели: перезапуск, готовые шаги придут из кэша. Остановку и ошибки документа не повторяем.
                    if isinstance(e, (Cancelled, RedactionLeak)) or job.attempt > self.auto_retry or job.stop_requested:
                        raise
                    job.write(f"Проверка не удалась: {_short(e)}. Перезапуск {job.attempt} из {self.auto_retry} "
                              f"через {self.retry_pause:.0f} с, готовые шаги возьмутся из кэша.")
                    job.attempt += 1
                    end = time.time() + self.retry_pause
                    while time.time() < end:
                        if job.stop_requested:
                            raise Cancelled("Проверка остановлена") from e
                        time.sleep(0.5)
            job.run_id = res.audit_dir.name
            job.status = "done"
            job.write(f"Готово за {job.elapsed:.0f} с")
        except Cancelled:
            job.status = "cancelled"
            job.write("Проверка остановлена. Готовые шаги сохранены в кэше: повторный запуск продолжит с места остановки.")
        except Exception as e:  # noqa: BLE001  ошибку отдаём клиенту, поток не должен падать молча
            job.error = _short(e)
            job.write(f"Ошибка: {e}")
            job.write(traceback.format_exc(limit=2))
            job.status = "error"
        finally:
            if acquired:
                self._slots.release()
            job.options.api_key = ""  # ключ не держим в памяти дольше проверки
            job.finished = time.time()


def _short(e: Exception) -> str:
    """Короткое сообщение для интерфейса: русский текст ошибки без технического хвоста провайдера."""
    msg = str(e).strip()
    for cut in (" 503 ", " 429 ", " 500 ", " {", " Последняя ошибка:"):
        i = msg.find(cut)
        if i > 10:
            msg = msg[:i].rstrip(" .:")
    return msg[:300]
