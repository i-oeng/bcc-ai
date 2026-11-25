"""Клиент Gemini: структурированный JSON-ответ, повторы при лимитах, журнал запросов.

Каждый запрос перед отправкой проходит Redactor.guard(): если в исходящем
тексте нашлись чувствительные данные, запрос не уходит. Все запросы и ответы
пишутся в папку прогона (audit), чтобы было видно, что именно ушло в модель.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .redact import Redactor

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    seconds: float = 0.0
    model_versions: set[str] = field(default_factory=set)


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        redactor: Redactor,
        audit_dir: Path | None = None,
        temperature: float | None = None,
        max_retries: int = 6,
        log=print,
    ):
        if not api_key:
            raise LLMError(
                "Не задан GEMINI_API_KEY. Получите ключ на https://aistudio.google.com/apikey "
                "и положите его в файл .env (см. .env.example)."
            )
        from google import genai

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.redactor = redactor
        self.audit_dir = audit_dir
        self.temperature = temperature
        self.max_retries = max_retries
        self.usage = Usage()
        self.log = log
        if audit_dir:
            audit_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, name: str, system: str, prompt: str, schema: type[T]) -> T:
        from google.genai import errors, types

        self.redactor.guard(system + "\n" + prompt)
        self.usage.calls += 1
        n = self.usage.calls
        if self.audit_dir:
            (self.audit_dir / f"{n:02d}_{name}.request.md").write_text(
                f"<!-- model: {self.model} -->\n# SYSTEM\n\n{system}\n\n# USER\n\n{prompt}\n", encoding="utf-8"
            )
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=self.temperature,
        )
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                resp = self.client.models.generate_content(model=self.model, contents=prompt, config=cfg)
            except errors.APIError as e:
                last_err = e
                code = getattr(e, "code", None)
                if code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    delay = _retry_delay(str(e), attempt)
                    self.log(f"    модель ответила {code}, повтор через {delay:.0f} с (попытка {attempt}/{self.max_retries})")
                    time.sleep(delay)
                    continue
                raise LLMError(_explain(e)) from e
            self.usage.seconds += time.time() - t0
            self._account(resp)
            text = resp.text or ""
            if self.audit_dir:
                (self.audit_dir / f"{n:02d}_{name}.response.json").write_text(text, encoding="utf-8")
            parsed = getattr(resp, "parsed", None)
            if isinstance(parsed, schema):
                return parsed
            try:
                return schema.model_validate_json(_strip_fences(text))
            except ValidationError as e:
                last_err = e
                if attempt < self.max_retries:
                    self.log(f"    ответ не прошёл проверку схемы, повтор (попытка {attempt}/{self.max_retries})")
                    continue
        raise LLMError(f"Шаг {name}: модель не вернула корректный ответ: {last_err}")

    def _account(self, resp) -> None:
        um = getattr(resp, "usage_metadata", None)
        if um:
            self.usage.prompt_tokens += um.prompt_token_count or 0
            self.usage.output_tokens += um.candidates_token_count or 0
            self.usage.thinking_tokens += getattr(um, "thoughts_token_count", 0) or 0
        mv = getattr(resp, "model_version", None)
        if mv:
            self.usage.model_versions.add(mv)

    def list_models(self) -> list[str]:
        out = []
        for m in self.client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                out.append(m.name.removeprefix("models/"))
        return out


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return m.group(1) if m else text


def _retry_delay(msg: str, attempt: int) -> float:
    m = re.search(r"retry(?:Delay)?['\"]?\s*[:=]?\s*['\"]?(\d+(?:\.\d+)?)s", msg, re.I)
    if m:
        return min(float(m.group(1)) + 2, 120)
    return min(5 * 2 ** (attempt - 1), 90)


def _explain(e) -> str:
    code = getattr(e, "code", None)
    msg = str(e)
    if code in (400,) and "API key" in msg:
        return "Ключ Gemini недействителен. Проверьте GEMINI_API_KEY в .env."
    if code == 403:
        return f"Доступ к модели запрещён (403). Проверьте ключ и модель. {msg}"
    if code == 404:
        return f"Модель не найдена (404). Посмотрите доступные: python -m validator --list-models. {msg}"
    if code == 429:
        return f"Исчерпан лимит запросов бесплатного тарифа (429). Подождите или смените модель. {msg}"
    return f"Ошибка Gemini API ({code}): {msg}"


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
