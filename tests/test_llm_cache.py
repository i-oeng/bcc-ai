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
