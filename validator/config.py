"""Настройки из переменных окружения и файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

DEFAULT_MODEL = "gemini-2.5-flash"


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass
class Config:
    api_key: str
    model: str
    temperature: float | None
    batch_size: int
    redact: bool
    redact_ner: bool
    redact_terms_file: str | None
    runs_dir: str
    tz: ZoneInfo

    @classmethod
    def load(cls, **overrides) -> "Config":
        root = Path(__file__).resolve().parent.parent
        load_dotenv(Path.cwd() / ".env")
        load_dotenv(root / ".env")
        temp = os.getenv("GEMINI_TEMPERATURE", "").strip()
        cfg = cls(
            api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            model=os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL,
            temperature=float(temp) if temp else None,
            batch_size=int(os.getenv("VALIDATOR_BATCH_SIZE", "8")),
            redact=_bool("REDACT_ENABLED", True),
            redact_ner=_bool("REDACT_NER", True),
            redact_terms_file=os.getenv("REDACT_TERMS_FILE", "").strip() or None,
            runs_dir=os.getenv("VALIDATOR_RUNS_DIR", "").strip() or str(root / "runs"),
            tz=ZoneInfo(os.getenv("VALIDATOR_TZ", "Asia/Almaty")),
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg
