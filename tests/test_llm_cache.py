from validator.llm import GeminiClient, Usage, _quota_exhausted
from validator.schemas import SetVerdict


def test_cache_roundtrip(tmp_path):
    c = GeminiClient.__new__(GeminiClient)  # без сети и ключа
    c.cache_dir = tmp_path
    v = SetVerdict(criterion="set_consistent", verdict="violation", comment="есть противоречие")
    assert c._cache_get("sys", "prompt", SetVerdict) is None
    c._cache_put("sys", "prompt", SetVerdict, v, "gemini-x")
    got, model = c._cache_get("sys", "prompt", SetVerdict)
    assert got == v and model == "gemini-x"
    assert c._cache_get("sys", "другой промпт", SetVerdict) is None


def test_quota_detection():
    per_day = "Quota exceeded ... limit: 20, model: gemini-3.8-flash, description: Generate content requests per day limit"
    per_min = "You exceeded your current quota, please check your plan and billing details. requests per minute, limit: 10"
    assert _quota_exhausted(per_day)
    assert not _quota_exhausted(per_min)
    assert _quota_exhausted("limit: 0, model: gemini-4-argon")


class _Resp:
    def __init__(self, text, model):
        self.text = text
        self.parsed = None
        self.model_version = model
        self.usage_metadata = None


class _Models:
    """Заглушка API: сценарий ответов по моделям."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def generate_content(self, model, contents, config):
        from google.genai import errors

        self.calls.append(model)
        step = self.script[model].pop(0) if self.script[model] else "ok"
        if step == "ok":
            return _Resp('{"criterion":"set_consistent","verdict":"ok","comment":"c"}', model)
        raise errors.APIError(step, {"error": {"code": step, "message": "limit: 20 per day" if step == 429 else "high demand"}})


def _client(script, monkeypatch):
    import time as _t

    from validator.redact import Redactor

    monkeypatch.setattr(_t, "sleep", lambda *_: None)
    c = GeminiClient.__new__(GeminiClient)
    c.models = list(script)
    c.redactor = Redactor(use_ner=False)
    c.audit_dir = None
    c.cache_dir = None
    c.temperature = None
    c.max_retries = 6
    c.overload_retries = 2
    c.rounds = 3
    c.exhausted = set()
    c.usage = Usage()
    c.log = lambda *_: None
    c.client = type("C", (), {"models": _Models(script)})()
    return c


def test_overload_falls_back_and_quota_is_remembered(monkeypatch):
    c = _client({"a": [503, 503], "b": [429], "c": []}, monkeypatch)
    out = c.generate("x", "sys", "prompt", SetVerdict)
    assert out.verdict == "ok" and c.usage.steps[-1] == ("x", "c")
    assert "b" in c.exhausted
    c.generate("y", "sys", "prompt 2", SetVerdict)  # «a» снова пробуется, «b» уже нет
    assert c.client.models.calls[-1] == "a" and c.client.models.calls.count("b") == 1


def test_all_overloaded_then_recovers_next_round(monkeypatch):
    c = _client({"a": [503, 503], "b": [503, 503]}, monkeypatch)
    assert c.generate("x", "sys", "p", SetVerdict).verdict == "ok"
    assert c.client.models.calls == ["a", "a", "b", "b", "a"]
