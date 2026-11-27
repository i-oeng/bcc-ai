"""Ключи и токены не уходят в модель, а настраиваются без неё."""

import pytest
from pydantic import BaseModel

from validator.llm import Backend, LLMClient, Reply
from validator.redact import RedactionLeak, Redactor

KEYS = [
    "AIzaSyD3m0x9Qk2LmN4pR7sT1uV8wX5yZ6aB0cD",          # Google
    "sk-ant-api03-Zx8Lq2Pw9Rt4Yu6Io1Kj3Hg5Fd7Sa0Mn",     # Anthropic
    "sk-or-v1-9f8e7d6c5b4a39281706f5e4d3c2b1a0",         # OpenRouter
    "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz012345",          # OpenAI
    "gsk_Q2w3E4r5T6y7U8i9O0pA1s2D3f4G5h6J7k8L",          # Groq
    "hf_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",           # Hugging Face
    "xai-Zq9Xw8Ce7Vr6Bt5Ny4Mu3Ki2Lo1Pa0Sd9Fg8",           # xAI
    "ghp_1234567890abcdefghijABCDEFGHIJ123456",          # GitHub
    "123456789:AAH4kq9xVb2nM7pL0sD3fG6hJ8kL1zX5cV9",     # Telegram bot
]


@pytest.mark.parametrize("key", KEYS)
def test_document_keys_are_masked_before_the_model(key):
    r = Redactor(use_ner=False)
    masked = r.redact(f"Для интеграции используется ключ {key} в заголовке.")
    assert key not in masked and "[СЕКРЕТ_" in masked
    r.guard(masked)  # после маскирования проверка пропускает
    with pytest.raises(RedactionLeak):
        r.guard(f"ключ {key}")  # без маскирования запрос не уйдёт


def test_private_key_block_is_masked():
    r = Redactor(use_ner=False)
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAx\n-----END RSA PRIVATE KEY-----"
    assert "MIIEowIBAAKCAQEAx" not in r.redact(f"Сертификат:\n{pem}\n")


class Answer(BaseModel):
    ok: bool


class Recorder(Backend):
    def __init__(self):
        self.prompts = []

    def call(self, model, system, prompt, schema):
        self.prompts.append(prompt)
        return Reply(Answer(ok=True), '{"ok": true}', model)


def test_configured_key_never_sent_even_with_masking_off(monkeypatch):
    """Ключ необычного формата (не ловится шаблонами) и маскирование выключено: запрос всё равно не уходит."""
    secret = "corp-7f3a9b2c-internal-llm-key-0042"
    monkeypatch.setenv("CORP_LLM_API_KEY", secret)
    backend = Recorder()
    client = LLMClient(backend, "m", Redactor(enabled=False), log=lambda *_: None)
    with pytest.raises(RedactionLeak, match="CORP_LLM_API_KEY"):
        client.generate("step", "system", f"Документ: ключ {secret}", Answer)
    assert backend.prompts == []  # модель не вызывалась
    assert client.generate("step", "system", "Документ без ключей", Answer).ok


def test_key_saved_without_model_and_utf16_kept(tmp_path, monkeypatch):
    from validator import config

    env = tmp_path / ".env"
    env.write_bytes("GEMINI_API_KEY=old\nLLM_PROVIDER=gemini\n".encode("utf-16"))
    monkeypatch.setattr(config, "env_path", lambda: env)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.save_env("ANTHROPIC_API_KEY", "sk-ant-api03-new-key-value-123456")
    config.save_env("GEMINI_API_KEY", "AIza-new")
    raw = env.read_bytes()
    assert raw.startswith((b"\xff\xfe", b"\xfe\xff"))  # кодировка PowerShell сохранена
    text = raw.decode("utf-16")
    assert "GEMINI_API_KEY=AIza-new" in text and "old" not in text
    assert "ANTHROPIC_API_KEY=sk-ant-api03-new-key-value-123456" in text and "LLM_PROVIDER=gemini" in text
    with pytest.raises(ValueError):
        config.save_env("BAD NAME", "x")
    with pytest.raises(ValueError):
        config.save_env("X_API_KEY", "a\nINJECTED=1")


def test_history_never_stores_keys(tmp_path, monkeypatch):
    pytest.importorskip("prompt_toolkit")
    from pathlib import Path

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from validator import shell

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SOME_API_KEY", "plain-looking-secret-value-99")
    with create_pipe_input() as inp:
        read = shell._make_reader(shell.Shell(), pt_input=inp, pt_output=DummyOutput())
    h = read.session.history
    for line in ["/findings крит", "/key", "мой ключ sk-ant-api03-Zx8Lq2Pw9Rt4Yu6Io1Kj3", "plain-looking-secret-value-99"]:
        h.store_string(line)
    saved = (tmp_path / ".bcc_validator_history").read_text(encoding="utf-8")
    assert "/findings" in saved
    assert "/key" not in saved and "sk-ant" not in saved and "plain-looking-secret" not in saved


def test_claude_request_shape(monkeypatch):
    """Claude: структурированный ответ через SDK, глубина размышлений high, серверный переход при отказе."""
    from types import SimpleNamespace

    from validator.llm import AnthropicBackend

    b = AnthropicBackend("sk-ant-test")
    seen = {}

    def fake_parse(**kw):
        seen.update(kw)
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"], parsed_output=Answer(ok=True),
                               content=[SimpleNamespace(type="text", text='{"ok": true}')],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=2, cache_read_input_tokens=0))

    monkeypatch.setattr(b.client.beta.messages, "parse", fake_parse)
    reply = b.call("claude-opus-5-5", "sys", "prompt", Answer)
    assert reply.result.ok
    assert seen["output_config"] == {"effort": "high"} and seen["output_format"] is Answer
    assert seen["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["betas"]
    assert "thinking" not in seen and "temperature" not in seen  # на Opus 5.5 их нельзя задавать
