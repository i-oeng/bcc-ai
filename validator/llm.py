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
    cached: int = 0
    steps: list[tuple[str, str]] = field(default_factory=list)  # шаг и модель, которая ответила


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
        fallbacks: list[str] | None = None,
        timeout_s: int = 240,
        cache_dir: Path | None = None,
    ):
        if not api_key:
            raise LLMError(
                "Не задан GEMINI_API_KEY. Получите ключ на https://aistudio.google.com/apikey "
                "и положите его в файл .env (см. .env.example)."
            )
        from google import genai

        self._genai = genai
        from google.genai import types

        # без таймаута зависший запрос держал бы весь прогон
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000))
        self.model = model
        self.models = list(dict.fromkeys([model] + [m for m in (fallbacks or []) if m]))
        self.overload_retries = 2
        self.rounds = 4  # кругов по цепочке при общей перегрузке
        self.exhausted: set[str] = set()
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.audit_dir = audit_dir
        self.temperature = temperature
        self.max_retries = max_retries
        self.usage = Usage()
        self.log = log
        if audit_dir:
            audit_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, name: str, system: str, prompt: str, schema: type[T]) -> T:
        """Один шаг конвейера. Модели пробуются по цепочке: основная, затем запасные.

        503/500 (перегрузка) и исчерпанная бесплатная квота переводят шаг на следующую
        модель; ограничение «запросов в минуту» пережидается на той же модели.
        """
        from google.genai import errors, types

        self.redactor.guard(system + "\n" + prompt)
        self.usage.calls += 1
        n = self.usage.calls
        if self.audit_dir:
            (self.audit_dir / f"{n:02d}_{name}.request.md").write_text(
                f"# SYSTEM\n\n{system}\n\n# USER\n\n{prompt}\n", encoding="utf-8"
            )
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=self.temperature,
        )
        cached = self._cache_get(system, prompt, schema)
        if cached is not None:
            result, model = cached
            self.usage.cached += 1
            self.usage.steps.append((name, model))
            if self.audit_dir:
                (self.audit_dir / f"{n:02d}_{name}.response.json").write_text(result.model_dump_json(indent=1), encoding="utf-8")
            self.log(f"    ответ взят из кэша ({model})")
            return result
        last_err: Exception | None = None
        for rnd in range(1, self.rounds + 1):
            live = [m for m in self.models if m not in self.exhausted]
            if not live:
                raise LLMError(
                    "У всех моделей цепочки исчерпана дневная квота бесплатного тарифа "
                    "(сброс в полночь по тихоокеанскому времени). Повторите прогон позже: "
                    f"готовые шаги возьмутся из кэша. Последняя ошибка: {last_err}"
                )
            for mi, model in enumerate(live):
                nxt = live[mi + 1] if mi + 1 < len(live) else None
                overloaded = 0
                for attempt in range(1, self.max_retries + 1):
                    t0 = time.time()
                    try:
                        resp = self.client.models.generate_content(model=model, contents=prompt, config=cfg)
                    except errors.APIError as e:
                        last_err = e
                        code = getattr(e, "code", None)
                        msg = str(e)
                        if (code == 429 and _quota_exhausted(msg)) or code == 404:
                            self.exhausted.add(model)  # до конца прогона эту модель не трогаем
                            why = "нет квоты" if code == 429 else "недоступна (404)"
                            self.log(f"    {model}: {why}" + (f", переключаюсь на {nxt}" if nxt else ""))
                            break
                        if code in (500, 502, 503, 504):
                            overloaded += 1
                            if overloaded >= self.overload_retries:
                                self.log(f"    {model}: перегружена ({code})" + (f", переключаюсь на {nxt}" if nxt else ""))
                                break
                        if code in (429, 500, 502, 503, 504):
                            if attempt >= self.max_retries:
                                break
                            delay = _retry_delay(msg, attempt)
                            self.log(f"    {model}: ответ {code}, повтор через {delay:.0f} с")
                            time.sleep(delay)
                            continue
                        raise LLMError(_explain(e)) from e
                    self.usage.seconds += time.time() - t0
                    self._account(resp)
                    text = resp.text or ""
                    if self.audit_dir:
                        (self.audit_dir / f"{n:02d}_{name}.response.json").write_text(text, encoding="utf-8")
                    parsed = getattr(resp, "parsed", None)
                    result = parsed if isinstance(parsed, schema) else None
                    if result is None:
                        try:
                            result = schema.model_validate_json(_strip_fences(text))
                        except ValidationError as e:
                            last_err = e
                            if attempt < self.max_retries:
                                self.log(f"    {model}: ответ не прошёл проверку схемы, повтор")
                                continue
                            break
                    answered = getattr(resp, "model_version", None) or model
                    self.usage.steps.append((name, answered))
                    self._cache_put(system, prompt, schema, result, answered)
                    return result
            if rnd < self.rounds and any(m not in self.exhausted for m in self.models):
                wait = 30 * rnd
                self.log(f"    все модели цепочки перегружены, пауза {wait} с (круг {rnd + 1}/{self.rounds})")
                time.sleep(wait)
        raise LLMError(f"Шаг {name}: ни одна модель не ответила. Повторите прогон позже, готовые шаги возьмутся из кэша. {last_err}")

    def _cache_key(self, system: str, prompt: str, schema) -> str:
        import hashlib

        payload = json.dumps([system, prompt, schema.__name__, schema.model_json_schema()], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def _cache_get(self, system, prompt, schema):
        if not self.cache_dir:
            return None
        p = self.cache_dir / f"{self._cache_key(system, prompt, schema)}.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return schema.model_validate(data["result"]), data["model"]
        except Exception:
            return None

    def _cache_put(self, system, prompt, schema, result, model) -> None:
        if not self.cache_dir:
            return
        p = self.cache_dir / f"{self._cache_key(system, prompt, schema)}.json"
        p.write_text(json.dumps({"model": model, "result": result.model_dump()}, ensure_ascii=False), encoding="utf-8")

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


def _quota_exhausted(msg: str) -> bool:
    """429 из-за нулевой или исчерпанной дневной квоты (а не лимита в минуту)."""
    return bool(re.search(r"limit: 0\b|per\s*day|perday|per_day", msg, re.I))


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
