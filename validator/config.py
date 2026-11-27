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
DEFAULT_FALLBACKS = "gemini-3.8-flash,gemini-4-hydrogen,gemini-3.5-flash-lite,gemma-4-31b-it"

# провайдер: переменные окружения и модель по умолчанию
PROVIDERS = {
    "gemini": {"title": "Google Gemini", "key": "GEMINI_API_KEY", "prefix": "GEMINI",
               "model": DEFAULT_MODEL, "fallbacks": DEFAULT_FALLBACKS},
    "openai": {"title": "Сервис, совместимый с OpenAI", "key": "OPENAI_API_KEY", "prefix": "OPENAI",
               "model": "gpt-5-mini", "fallbacks": ""},
    "anthropic": {"title": "Anthropic Claude", "key": "ANTHROPIC_API_KEY", "prefix": "ANTHROPIC",
                  "model": "claude-opus-5-5", "fallbacks": ""},
}


def provider_defaults(provider: str) -> dict:
    """Модель, запасные модели и наличие ключа в .env для провайдера (для интерфейса и CLI)."""
    p = PROVIDERS[provider]
    return {
        "id": provider, "title": p["title"],
        "model": os.getenv(f"{p['prefix']}_MODEL", "").strip() or p["model"],
        "fallbacks": [m.strip() for m in (os.getenv(f"{p['prefix']}_FALLBACK_MODELS") or p["fallbacks"]).split(",") if m.strip()],
        "has_env_key": bool(os.getenv(p["key"], "").strip()),
        "base_url": os.getenv("OPENAI_BASE_URL", "").strip() if provider == "openai" else "",
    }


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


def env_path() -> Path:
    """Файл .env проекта (рядом с README)."""
    return Path(__file__).resolve().parent.parent / ".env"


def save_env(name: str, value: str) -> Path:
    """Записать переменную в .env, сохранив кодировку файла (UTF-16 от PowerShell или UTF-8).

    Значение уходит только на диск и в окружение процесса: в модель и в журналы оно не попадает.
    """
    import re as _re

    if not _re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise ValueError(f"Недопустимое имя переменной: {name!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("Значение не может содержать перевод строки")
    path = env_path()
    raw = path.read_bytes() if path.is_file() else b""
    utf16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    text = raw.decode("utf-16") if utf16 else raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    line = f"{name}={value}"
    for i, ln in enumerate(lines):
        if _re.match(rf"\s*(export\s+)?{name}\s*=", ln):
            lines[i] = line
            break
    else:
        lines.append(line)
    out = "\n".join(lines) + "\n"
    path.write_bytes(out.encode("utf-16") if utf16 else out.encode("utf-8"))
    try:
        os.chmod(path, 0o600)  # только владелец (на Windows без эффекта)
    except OSError:
        pass
    os.environ[name] = value
    return path


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
    cache_dir: str | None
    tz: ZoneInfo
    provider: str = "gemini"
    base_url: str | None = None  # только для OpenAI-совместимых серверов
    standards: list[str] | None = None  # стандарты проверки (criteria.profile); None: по умолчанию
    parallel: int = 6  # сколько запросов к модели одной проверки идут одновременно

    @classmethod
    def load(cls, **overrides) -> "Config":
        root = Path(__file__).resolve().parent.parent
        for env in (Path.cwd() / ".env", root / ".env"):
            _load_env(env)
        provider = (overrides.get("provider") or os.getenv("LLM_PROVIDER", "") or "gemini").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"Неизвестный провайдер {provider!r}. Доступны: {', '.join(PROVIDERS)}")
        p, d = PROVIDERS[provider], provider_defaults(provider)
        temp = (os.getenv(f"{p['prefix']}_TEMPERATURE", "") or os.getenv("GEMINI_TEMPERATURE", "")).strip()
        standards = [s.strip() for s in os.getenv("VALIDATOR_STANDARDS", "").split(",") if s.strip()] or None
        cfg = cls(
            api_key=os.getenv(p["key"], "").strip(),
            model=d["model"],
            fallbacks=d["fallbacks"],
            temperature=float(temp) if temp else None,
            batch_size=int(os.getenv("VALIDATOR_BATCH_SIZE", "12")),
            redact=_bool("REDACT_ENABLED", True),
            redact_ner=_bool("REDACT_NER", True),
            redact_terms_file=os.getenv("REDACT_TERMS_FILE", "").strip() or None,
            runs_dir=os.getenv("VALIDATOR_RUNS_DIR", "").strip() or str(root / "runs"),
            cache_dir=None if not _bool("VALIDATOR_CACHE", True) else (os.getenv("VALIDATOR_CACHE_DIR", "").strip() or str(root / ".cache")),
            tz=ZoneInfo(os.getenv("VALIDATOR_TZ", "Asia/Almaty")),
            provider=provider,
            base_url=d["base_url"] or None,
            standards=standards,
            parallel=int(os.getenv("VALIDATOR_PARALLEL", "6")),
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg
