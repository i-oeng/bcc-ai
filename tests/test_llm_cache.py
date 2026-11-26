"""Общий клиент модели: кэш, распознавание квоты, цепочка запасных моделей, остановка, параллельность."""

import threading

import pytest

from validator.llm import Backend, Cancelled, LLMClient, ProviderError, Reply, _quota_exhausted
from validator.redact import Redactor
from validator.schemas import SetVerdict

OK = '{"criterion":"set_consistent","verdict":"ok","comment":"c"}'


class ScriptBackend(Backend):
    """Заглушка провайдера: сценарий ответов по моделям (ok, quota, notfound, overload, ratelimit, bad)."""

    name = "stub"

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self._lock = threading.Lock()

    def call(self, model, system, prompt, schema):
        with self._lock:
            self.calls.append(model)
            step = self.script[model].pop(0) if self.script[model] else "ok"
        if step == "ok":
            return Reply(None, OK, model)
        if step == "bad":
            return Reply(None, "{not json", model)
        raise ProviderError(step, f"{step} error", 503 if step == "overload" else 429)


def _client(script, monkeypatch, **kw):
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda *_: None)
    c = LLMClient(ScriptBackend(script), list(script)[0], Redactor(use_ner=False), fallbacks=list(script)[1:],
                  log=lambda *_: None, **kw)
    c.rounds = 3
    c._sleep = lambda s: c.check_stop()  # паузы между повторами в тестах не ждём
    return c


def test_cache_roundtrip(tmp_path):
    c = LLMClient(ScriptBackend({"m": []}), "m", Redactor(use_ner=False), cache_dir=tmp_path, log=lambda *_: None)
    v = SetVerdict(criterion="set_consistent", verdict="violation", comment="есть противоречие")
    assert c._cache_get("sys", "prompt", SetVerdict) is None
    c._cache_put("sys", "prompt", SetVerdict, v, "model-x")
    got, model = c._cache_get("sys", "prompt", SetVerdict)
    assert got == v and model == "model-x"
    assert c._cache_get("sys", "другой промпт", SetVerdict) is None
    # второй запрос с тем же промптом не идёт к провайдеру
    c.generate("x", "sys", "p", SetVerdict)
    c.generate("x", "sys", "p", SetVerdict)
    assert c.backend.calls == ["m"] and c.usage.cached == 1


def test_quota_detection():
    per_day = "Quota exceeded ... limit: 20, model: gemini-3.8-flash, description: Generate content requests per day limit"
    per_min = "You exceeded your current quota, please check your plan and billing details. requests per minute, limit: 10"
    assert _quota_exhausted(per_day)
    assert not _quota_exhausted(per_min)
    assert _quota_exhausted("limit: 0, model: gemini-4-argon")


def test_overload_falls_back_and_quota_is_remembered(monkeypatch):
    c = _client({"a": ["overload", "overload"], "b": ["quota"], "c": []}, monkeypatch)
    out = c.generate("x", "sys", "prompt", SetVerdict)
    assert out.verdict == "ok" and c.usage.steps[-1] == ("x", "c")
    assert "b" in c.exhausted
    c.generate("y", "sys", "prompt 2", SetVerdict)  # «a» снова пробуется, «b» уже нет
    assert c.backend.calls[-1] == "a" and c.backend.calls.count("b") == 1


def test_all_overloaded_then_recovers_next_round(monkeypatch):
    c = _client({"a": ["overload", "overload"], "b": ["overload", "overload"]}, monkeypatch)
    assert c.generate("x", "sys", "p", SetVerdict).verdict == "ok"
    assert c.backend.calls == ["a", "a", "b", "b", "a"]


def test_bad_json_is_retried(monkeypatch):
    c = _client({"a": ["bad"]}, monkeypatch)
    assert c.generate("x", "sys", "p", SetVerdict).verdict == "ok"
    assert c.backend.calls == ["a", "a"]


def test_stop_interrupts(monkeypatch):
    c = _client({"a": []}, monkeypatch, should_stop=lambda: True)
    with pytest.raises(Cancelled):
        c.generate("x", "sys", "p", SetVerdict)
    assert c.backend.calls == []


def test_parallel_calls_are_counted_safely(monkeypatch):
    c = _client({"a": []}, monkeypatch)
    threads = [threading.Thread(target=c.generate, args=(f"s{i}", "sys", f"p{i}", SetVerdict)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.usage.calls == 20 and len(c.usage.steps) == 20


def test_hedge_takes_first_answer(monkeypatch):
    """Первый запрос завис: через hedge_after уходит повторный, его ответ и принимается."""
    import time as _t

    class Hanging(ScriptBackend):
        def call(self, model, system, prompt, schema):
            with self._lock:
                self.calls.append(model)
                first = len(self.calls) == 1
            if first:
                _t.sleep(3)
            return Reply(None, OK, model + ("-slow" if first else "-hedge"))

    c = LLMClient(Hanging({"m": []}), "m", Redactor(use_ner=False), log=lambda *_: None, hedge_after=0.3)
    t0 = _t.time()
    c.generate("x", "sys", "p", SetVerdict)
    assert _t.time() - t0 < 2 and c.usage.steps[-1] == ("x", "m-hedge") and c.usage.calls == 2
