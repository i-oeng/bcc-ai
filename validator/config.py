"""Настройки из переменных окружения и файла .env."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# gemini-3.6-flash: стабильно доступна на бесплатном тарифе; у 3.8-flash лимит 20 запросов в сутки
DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_FALLBACKS = "gemini-3.8-flash,gemini-4-hydrogen,gemma-4-31b-it"


def _load_env(path: Path) -> None:
    """.env в любой кодировке: PowerShell 5 по умолчанию пишет UTF-16."""
    if not path.is_file():
        return
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="replace")
    load_dotenv(stream=io.StringIO(text))


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass
class Config:
    api_key: str
    model: str
    fallbacks: list[str]
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
        for env in (Path.cwd() / ".env", root / ".env"):
            _load_env(env)
        temp = os.getenv("GEMINI_TEMPERATURE", "").strip()
        cfg = cls(
            api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            model=os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL,
            fallbacks=[m.strip() for m in (os.getenv("GEMINI_FALLBACK_MODELS") or DEFAULT_FALLBACKS).split(",") if m.strip()],
            temperature=float(temp) if temp else None,
            batch_size=int(os.getenv("VALIDATOR_BATCH_SIZE", "12")),
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
