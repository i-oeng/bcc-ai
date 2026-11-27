"""Клиент модели: структурированный JSON-ответ, цепочка запасных моделей, кэш, журнал запросов.

Провайдеры (бэкенды): Gemini, любой OpenAI-совместимый API (OpenAI, OpenRouter,
DeepSeek, Groq, локальный vLLM или Ollama) и Anthropic Claude. Логика повторов,
кэша, журнала и маскирования общая и не зависит от провайдера.

Каждый запрос перед отправкой проходит Redactor.guard(): если в исходящем
тексте нашлись чувствительные данные, запрос не уходит. Все запросы и ответы
пишутся в папку проверки (audit), чтобы было видно, что именно ушло в модель.
Клиент потокобезопасен: конвейер шлёт независимые шаги параллельно.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .redact import Redactor

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    """Проверку остановил пользователь. Готовые шаги остаются в кэше."""


class ProviderError(Exception):
    """Ошибка провайдера, приведённая к общему виду.

    kind: quota (дневная квота или кредит исчерпаны), notfound (модели нет),
    overload (5xx, сеть, таймаут), ratelimit (лимит в минуту), fatal (ключ, запрос).
    """

    def __init__(self, kind: str, message: str, code=None, retry_after: float | None = None):
        super().__init__(message)
        self.kind, self.code, self.retry_after = kind, code, retry_after


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


@dataclass
class Reply:
    result: BaseModel | None  # уже разобранный ответ, если провайдер умеет; иначе разбираем text
    text: str
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


# ---------- провайдеры ----------


class Backend:
    name = "base"
    title = "base"

    def call(self, model: str, system: str, prompt: str, schema: type[BaseModel]) -> Reply:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []


class GeminiBackend(Backend):
    name = "gemini"
    title = "Google Gemini"

    def __init__(self, api_key: str, temperature: float | None = None, timeout_s: int = 180, thinking: str | None = None):
        from google import genai
        from google.genai import types

        self.types = types
        # без таймаута зависший запрос держал бы всю проверку
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000))
        self.temperature = temperature
        self.thinking = (thinking or "").strip().lower() or None
        self.no_thinking: set[str] = set()  # модели, которые отвергли настройку размышлений

    def call(self, model, system, prompt, schema):
        from google.genai import errors

        kw = dict(system_instruction=system, response_mime_type="application/json", response_schema=schema,
                  temperature=self.temperature)
        use_thinking = self.thinking and model.startswith("gemini-") and model not in self.no_thinking
        if use_thinking:
            kw["thinking_config"] = self.types.ThinkingConfig(thinking_level=self.thinking)
        try:
            resp = self.client.models.generate_content(model=model, contents=prompt,
                                                       config=self.types.GenerateContentConfig(**kw))
        except errors.APIError as e:
            code, msg = getattr(e, "code", None), str(e)
            if code == 400 and use_thinking and "think" in msg.lower():
                self.no_thinking.add(model)  # модель не поддерживает уровень размышлений: повторим без него
                return self.call(model, system, prompt, schema)
            raise _gemini_error(e) from e
        um = getattr(resp, "usage_metadata", None)
        parsed = getattr(resp, "parsed", None)
        return Reply(parsed if isinstance(parsed, schema) else None, resp.text or "",
                     getattr(resp, "model_version", None) or model,
                     (um.prompt_token_count or 0) if um else 0, (um.candidates_token_count or 0) if um else 0,
                     (getattr(um, "thoughts_token_count", 0) or 0) if um else 0)

    def list_models(self):
        out = []
        for m in self.client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                out.append(m.name.removeprefix("models/"))
        return out


def _gemini_error(e) -> ProviderError:
    code, msg = getattr(e, "code", None), str(e)
    if code == 429 and _quota_exhausted(msg):
        return ProviderError("quota", msg, code)
    if code == 404:
        return ProviderError("notfound", msg, code)
    if code in (500, 502, 503, 504):
        return ProviderError("overload", msg, code)
    if code == 429:
        return ProviderError("ratelimit", msg, code, _retry_delay(msg, 1))
    return ProviderError("fatal", _explain_gemini(e), code)


class OpenAIBackend(Backend):
    """OpenAI и любой совместимый API (base_url): OpenRouter, DeepSeek, Groq, vLLM, Ollama."""

    name = "openai"
    title = "OpenAI-совместимый API"

    def __init__(self, api_key: str, base_url: str | None = None, temperature: float | None = None, timeout_s: int = 180):
        import openai

        self.openai = openai
        self.client = openai.OpenAI(api_key=api_key or "none", base_url=base_url or None, timeout=timeout_s, max_retries=0)
        self.temperature = temperature
        # строгая JSON-схема есть у OpenAI; у совместимых серверов поддержка разная, поэтому там режим JSON
        self.structured = not base_url

    def call(self, model, system, prompt, schema):
        o = self.openai
        try:
            if self.structured:
                try:
                    resp = self.client.chat.completions.parse(
                        model=model, response_format=schema, temperature=self.temperature,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
                except (o.BadRequestError, TypeError, ValueError) as e:
                    if isinstance(e, o.BadRequestError) and "schema" not in str(e).lower() and "response_format" not in str(e).lower():
                        raise
                    self.structured = False  # схема не принята: переходим на режим JSON с описанием схемы
                    return self.call(model, system, prompt, schema)
                msg = resp.choices[0].message
                if getattr(msg, "refusal", None):
                    raise ProviderError("fatal", f"Модель отказалась отвечать: {msg.refusal}")
                result, text = msg.parsed, msg.content or ""
            else:
                sys_json = (system + "\n\nОтветь одним JSON-объектом строго по этой JSON-схеме, без пояснений:\n"
                            + json.dumps(schema.model_json_schema(), ensure_ascii=False))
                resp = self.client.chat.completions.create(
                    model=model, temperature=self.temperature, response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": sys_json}, {"role": "user", "content": prompt}])
                result, text = None, resp.choices[0].message.content or ""
        except o.LengthFinishReasonError as e:
            raise ProviderError("overload", f"Ответ обрезан по длине: {e}") from e
        except o.RateLimitError as e:
            body = str(e)
            if "insufficient_quota" in body or "billing" in body.lower():
                raise ProviderError("quota", body, 429) from e
            raise ProviderError("ratelimit", body, 429, _retry_after_header(e)) from e
        except o.NotFoundError as e:
            raise ProviderError("notfound", str(e), 404) from e
        except o.AuthenticationError as e:
            raise ProviderError("fatal", "Ключ OpenAI-совместимого API недействителен (401). Проверьте ключ.", 401) from e
        except o.PermissionDeniedError as e:
            raise ProviderError("fatal", f"Доступ к модели запрещён (403): {e}", 403) from e
        except (o.APIConnectionError, o.APITimeoutError, o.InternalServerError) as e:
            raise ProviderError("overload", str(e), getattr(e, "status_code", None)) from e
        except o.APIStatusError as e:
            kind = "overload" if (e.status_code or 0) >= 500 else "fatal"
            raise ProviderError(kind, f"Ошибка API ({e.status_code}): {e}", e.status_code) from e
        u = getattr(resp, "usage", None)
        return Reply(result if isinstance(result, schema) else None, text, getattr(resp, "model", None) or model,
                     getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)

    def list_models(self):
        return sorted(m.id for m in self.client.models.list())


class AnthropicBackend(Backend):
    name = "anthropic"
    title = "Anthropic Claude"
    # модели, у которых есть серверный переход на запасную модель при отказе
    FALLBACK_MODELS = ("claude-fable-5", "claude-opus-5", "claude-sonnet-5-5")

    def __init__(self, api_key: str, timeout_s: int = 300):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=0)

    def call(self, model, system, prompt, schema):
        a = self.anthropic
        kw = dict(model=model, max_tokens=16000, system=system, output_format=schema,
                  messages=[{"role": "user", "content": prompt}])
        try:
            if model.startswith(self.FALLBACK_MODELS):
                # при отказе по политике безопасности API сам повторит запрос на подходящей модели
                resp = self.client.beta.messages.parse(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
            else:
                resp = self.client.messages.parse(**kw)
        except a.RateLimitError as e:
            raise ProviderError("ratelimit", str(e), 429, _retry_after_header(e)) from e
        except a.NotFoundError as e:
            raise ProviderError("notfound", str(e), 404) from e
        except a.AuthenticationError as e:
            raise ProviderError("fatal", "Ключ Anthropic недействителен (401). Проверьте ANTHROPIC_API_KEY.", 401) from e
        except a.PermissionDeniedError as e:
            raise ProviderError("fatal", f"Доступ к модели запрещён (403): {e}", 403) from e
        except a.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise ProviderError("quota", f"На счёте Anthropic закончился кредит: {e}", 400) from e
            raise ProviderError("fatal", f"Запрос отклонён (400): {e}", 400) from e
        except (a.APIConnectionError, a.APITimeoutError) as e:
            raise ProviderError("overload", str(e)) from e
        except a.APIStatusError as e:
            kind = "overload" if (e.status_code or 0) >= 500 else "fatal"
            raise ProviderError(kind, f"Ошибка Anthropic API ({e.status_code}): {e}", e.status_code) from e
        if resp.stop_reason == "refusal":
            raise ProviderError("fatal", "Модель отказалась отвечать (refusal)")
        if resp.stop_reason == "max_tokens":
            raise ProviderError("overload", "Ответ обрезан по длине (max_tokens)")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        parsed = getattr(resp, "parsed_output", None)
        return Reply(parsed if isinstance(parsed, schema) else None, text, getattr(resp, "model", None) or model,
                     resp.usage.input_tokens or 0, resp.usage.output_tokens or 0)

    def list_models(self):
        return [m.id for m in self.client.models.list()]


PROVIDERS = {"gemini": GeminiBackend, "openai": OpenAIBackend, "anthropic": AnthropicBackend}


def make_backend(provider: str, api_key: str, base_url: str | None = None, temperature: float | None = None) -> Backend:
    if not api_key and not (provider == "openai" and base_url):
        hint = {"gemini": "GEMINI_API_KEY (ключ: https://aistudio.google.com/apikey)", "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY (ключ: https://platform.claude.com)"}.get(provider, "ключ API")
        raise LLMError(f"Не задан {hint}. Положите его в файл .env (см. .env.example) или впишите в настройках интерфейса.")
    if provider == "gemini":
        return GeminiBackend(api_key, temperature, thinking=os.getenv("GEMINI_THINKING", "low"))
    if provider == "openai":
        return OpenAIBackend(api_key, base_url, temperature)
    if provider == "anthropic":
        return AnthropicBackend(api_key)
    raise LLMError(f"Неизвестный провайдер {provider!r}. Доступны: {', '.join(PROVIDERS)}")


# ---------- общий клиент ----------


class LLMClient:
    def __init__(
        self,
        backend: Backend,
        model: str,
        redactor: Redactor,
        audit_dir: Path | None = None,
        log=print,
        fallbacks: list[str] | None = None,
        cache_dir: Path | None = None,
        should_stop: Callable[[], bool] | None = None,
        max_retries: int = 6,
        hedge_after: float | None = None,
    ):
        self.backend = backend
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
        self.max_retries = max_retries
        self.usage = Usage()
        self.log = log
        self.should_stop = should_stop
        # хедж: если ответа нет дольше hedge_after секунд, тот же запрос уходит второй раз, берётся первый ответ.
        # У бесплатных моделей время ответа скачет от 40 с до нескольких минут, хедж срезает такие хвосты.
        self.hedge_after = float(os.getenv("VALIDATOR_HEDGE_SECONDS", "60")) if hedge_after is None else hedge_after
        self._lock = threading.Lock()
        if audit_dir:
            audit_dir.mkdir(parents=True, exist_ok=True)

    # ---- остановка ----

    def check_stop(self) -> None:
        if self.should_stop and self.should_stop():
            raise Cancelled("Проверка остановлена")

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.check_stop()
            time.sleep(min(0.5, max(0.0, end - time.time())))

    # ---- шаг ----

    def generate(self, name: str, system: str, prompt: str, schema: type[T]) -> T:
        """Один шаг конвейера. Модели пробуются по цепочке: основная, затем запасные.

        Перегрузка (5xx, сеть) и исчерпанная квота переводят шаг на следующую модель;
        ограничение «запросов в минуту» пережидается на той же модели.
        """
        self.check_stop()
        self.redactor.guard(system + "\n" + prompt)
        with self._lock:
            self.usage.calls += 1
            n = self.usage.calls
        if self.audit_dir:
            (self.audit_dir / f"{n:02d}_{name}.request.md").write_text(
                f"# SYSTEM\n\n{system}\n\n# USER\n\n{prompt}\n", encoding="utf-8"
            )
        cached = self._cache_get(system, prompt, schema)
        if cached is not None:
            result, model = cached
            with self._lock:
                self.usage.cached += 1
                self.usage.steps.append((name, model))
            if self.audit_dir:
                (self.audit_dir / f"{n:02d}_{name}.response.json").write_text(result.model_dump_json(indent=1), encoding="utf-8")
            self.log(f"    {name}: ответ взят из кэша ({model})")
            return result
        last_err: Exception | None = None
        for rnd in range(1, self.rounds + 1):
            live = [m for m in self.models if m not in self.exhausted]
            if not live:
                raise LLMError(
                    "У всех моделей цепочки исчерпана квота. Повторите проверку позже или смените провайдера: "
                    f"готовые шаги возьмутся из кэша. Последняя ошибка: {last_err}"
                )
            for mi, model in enumerate(live):
                nxt = live[mi + 1] if mi + 1 < len(live) else None
                overloaded = 0
                for attempt in range(1, self.max_retries + 1):
                    self.check_stop()
                    t0 = time.time()
                    try:
                        reply = self._call(name, model, system, prompt, schema)
                    except ProviderError as e:
                        last_err = e
                        if e.kind in ("quota", "notfound"):
                            self.exhausted.add(model)  # до конца проверки эту модель не трогаем
                            why = "нет квоты" if e.kind == "quota" else "недоступна (404)"
                            self.log(f"    {model}: {why}" + (f", переключаюсь на {nxt}" if nxt else ""))
                            break
                        if e.kind == "overload":
                            overloaded += 1
                            if overloaded >= self.overload_retries:
                                self.log(f"    {model}: перегружена ({e.code or 'сеть'})" + (f", переключаюсь на {nxt}" if nxt else ""))
                                break
                        if e.kind in ("overload", "ratelimit"):
                            if attempt >= self.max_retries:
                                break
                            delay = e.retry_after or min(5 * 2 ** (attempt - 1), 90)
                            self.log(f"    {model}: ответ {e.code or 'нет'}, повтор через {delay:.0f} с")
                            self._sleep(delay)
                            continue
                        raise LLMError(str(e)) from e
                    with self._lock:
                        self.usage.seconds += time.time() - t0
                        self.usage.prompt_tokens += reply.prompt_tokens
                        self.usage.output_tokens += reply.output_tokens
                        self.usage.thinking_tokens += reply.thinking_tokens
                        self.usage.model_versions.add(reply.model)
                    if self.audit_dir:
                        (self.audit_dir / f"{n:02d}_{name}.response.json").write_text(reply.text, encoding="utf-8")
                    result = reply.result
                    if result is None:
                        try:
                            result = schema.model_validate_json(_strip_fences(reply.text))
                        except ValidationError as e:
                            last_err = e
                            if attempt < self.max_retries:
                                self.log(f"    {model}: ответ не прошёл проверку схемы, повтор")
                                continue
                            break
                    with self._lock:
                        self.usage.steps.append((name, reply.model))
                    self._cache_put(system, prompt, schema, result, reply.model)
                    return result
            if rnd < self.rounds and any(m not in self.exhausted for m in self.models):
                wait = 30 * rnd
                self.log(f"    все модели цепочки перегружены, пауза {wait} с (круг {rnd + 1}/{self.rounds})")
                self._sleep(wait)
        raise LLMError(f"Шаг {name}: ни одна модель не ответила. Повторите проверку позже, готовые шаги возьмутся из кэша. {last_err}")

    def _call(self, name: str, model: str, system: str, prompt: str, schema) -> Reply:
        """Вызов провайдера с хеджем и проверкой остановки во время ожидания."""
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="call")
        try:
            first = pool.submit(self.backend.call, model, system, prompt, schema)
            futures = {first}
            started, hedged = time.time(), False
            errors: list[BaseException] = []
            while futures:
                self.check_stop()
                done, futures = wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
                for f in done:
                    try:
                        return f.result()
                    except Exception as e:  # noqa: BLE001  ждём второй запрос, если он есть
                        errors.append(e)
                if not hedged and futures and self.hedge_after and time.time() - started > self.hedge_after:
                    hedged = True
                    with self._lock:
                        self.usage.calls += 1
                    self.log(f"    {name}: {model} молчит дольше {self.hedge_after:.0f} с, отправляю повторный запрос параллельно")
                    futures.add(pool.submit(self.backend.call, model, system, prompt, schema))
            raise errors[0]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # ---- кэш ----

    def _cache_key(self, system: str, prompt: str, schema) -> str:
        items = [system, prompt, schema.__name__, schema.model_json_schema()]
        if getattr(self, "backend", None) is not None and self.backend.name != "gemini":
            items.append(self.backend.name)  # ответы разных провайдеров не смешиваем
        payload = json.dumps(items, ensure_ascii=False)
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
        tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"model": model, "result": result.model_dump()}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)  # атомарно: параллельные шаги и проверки не видят полузаписанный файл

    def list_models(self) -> list[str]:
        return self.backend.list_models()


def make_client(cfg, redactor: Redactor, audit_dir: Path | None, log=print, should_stop=None) -> LLMClient:
    backend = make_backend(cfg.provider, cfg.api_key, cfg.base_url, cfg.temperature)
    return LLMClient(backend, cfg.model, redactor, audit_dir, log=log, fallbacks=cfg.fallbacks,
                     cache_dir=Path(cfg.cache_dir) if cfg.cache_dir else None, should_stop=should_stop)


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


def _retry_after_header(e) -> float | None:
    try:
        v = e.response.headers.get("retry-after")
        return min(float(v) + 1, 120) if v else None
    except Exception:
        return None


def _explain_gemini(e) -> str:
    code = getattr(e, "code", None)
    msg = str(e)
    if code in (400,) and "API key" in msg:
        return "Ключ Gemini недействителен. Проверьте GEMINI_API_KEY в .env."
    if code == 403:
        return f"Доступ к модели запрещён (403). Проверьте ключ и модель. {msg}"
    return f"Ошибка Gemini API ({code}): {msg}"


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
