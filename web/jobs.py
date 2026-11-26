"""Фоновые проверки: очередь, параллельный запуск нескольких документов, остановка.

Полная проверка идёт минуты, поэтому HTTP-запрос только ставит задачу, а
проверка живёт в отдельном потоке. Одновременно идут до VALIDATOR_MAX_JOBS
проверок (по умолчанию 3), остальные ждут в очереди. Остановка срабатывает
между шагами и во время пауз; ответы, полученные до остановки, остаются в
кэше, и повторный запуск продолжит с того же места.
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
from validator.llm import Cancelled

from .services import RunOptions, make_config


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

    def public(self, log_offset: int = 0, with_log: bool = True) -> dict:
        lines = self.log_since(log_offset) if with_log else []
        last = self.log_since(max(0, len(self.log) - 1))
        return {"id": self.jid, "status": self.status, "document": self.input_path.name,
                "with_answers": self.answers_path is not None, "fix": self.options.fix,
                "provider": self.options.provider, "model": self.options.model,
                "elapsed": round(self.elapsed, 1), "run_id": self.run_id, "error": self.error,
                "stop_requested": self.stop_requested, "last": last[0] if last else "",
                "log": lines, "log_next": log_offset + len(lines)}


class JobRegistry:
    def __init__(self, client_factory=None, max_parallel: int | None = None):
        self.jobs: dict[str, Job] = {}
        self.client_factory = client_factory  # для тестов: поддельная модель
        self.max_parallel = max_parallel or int(os.getenv("VALIDATOR_MAX_JOBS", "3"))
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
            res = pipeline.run(
                str(job.input_path), make_config(job.options), log=job.write,
                answers_path=str(job.answers_path) if job.answers_path else None, fix=job.options.fix,
                client_factory=self.client_factory, should_stop=lambda: job.stop_requested,
            )
            job.run_id = res.audit_dir.name
            job.status = "done"
            job.write(f"Готово за {job.elapsed:.0f} с")
        except Cancelled:
            job.status = "cancelled"
            job.write("Проверка остановлена. Готовые шаги сохранены в кэше: повторный запуск продолжит с места остановки.")
        except Exception as e:  # noqa: BLE001  ошибку отдаём клиенту, поток не должен падать молча
            job.error = f"{type(e).__name__}: {e}"
            job.write(traceback.format_exc(limit=3))
            job.status = "error"
        finally:
            if acquired:
                self._slots.release()
            job.options.api_key = ""  # ключ не держим в памяти дольше проверки
            job.finished = time.time()
