"""Фоновые прогоны конвейера.

Полная проверка идёт минуты, поэтому HTTP-запрос только ставит задачу, а
прогон живёт в отдельном потоке. Клиент опрашивает статус и журнал задачи.
Одновременно идёт не больше одного прогона: бесплатная квота модели общая.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from validator import pipeline

from .services import RunOptions, make_config


class Busy(RuntimeError):
    pass


@dataclass
class Job:
    jid: str
    input_path: Path
    options: RunOptions
    answers_path: Path | None = None
    status: str = "running"  # running | done | error
    log: list[str] = field(default_factory=list)
    run_id: str = ""  # папка прогона в runs/, когда готово
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def write(self, *parts) -> None:
        with self._lock:
            self.log.append(" ".join(str(p) for p in parts))

    def log_since(self, offset: int = 0) -> list[str]:
        with self._lock:
            return self.log[offset:]

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started

    def public(self, log_offset: int = 0) -> dict:
        lines = self.log_since(log_offset)
        return {"id": self.jid, "status": self.status, "document": self.input_path.name,
                "with_answers": self.answers_path is not None, "fix": self.options.fix,
                "elapsed": round(self.elapsed, 1), "run_id": self.run_id, "error": self.error,
                "log": lines, "log_next": log_offset + len(lines)}


class JobRegistry:
    def __init__(self, client_factory=None):
        self.jobs: dict[str, Job] = {}
        self.client_factory = client_factory  # для тестов: поддельная модель
        self._lock = threading.Lock()

    def start(self, input_path: str | Path, options: RunOptions, answers_path: str | Path | None = None) -> Job:
        with self._lock:
            if self.running():
                raise Busy("Уже идёт другой прогон")
            job = Job(uuid.uuid4().hex[:8], Path(input_path), options, Path(answers_path) if answers_path else None)
            self.jobs[job.jid] = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"validator-{job.jid}").start()
        return job

    def get(self, jid: str) -> Job | None:
        return self.jobs.get(jid)

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running"]

    def _run(self, job: Job) -> None:
        try:
            res = pipeline.run(
                str(job.input_path), make_config(job.options), log=job.write,
                answers_path=str(job.answers_path) if job.answers_path else None, fix=job.options.fix,
                client_factory=self.client_factory,
            )
            job.run_id = res.audit_dir.name
            job.status = "done"
        except Exception as e:  # noqa: BLE001  ошибку отдаём клиенту, поток не должен падать молча
            job.error = f"{type(e).__name__}: {e}"
            job.write(traceback.format_exc(limit=3))
            job.status = "error"
        finally:
            job.options.api_key = ""  # ключ не держим в памяти дольше прогона
            job.finished = time.time()
