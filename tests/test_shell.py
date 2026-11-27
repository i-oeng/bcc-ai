"""Интерактивный режим: автодополнение и подсказки (без настоящего терминала)."""

import pytest

pt = pytest.importorskip("prompt_toolkit")

from prompt_toolkit.buffer import Buffer  # noqa: E402
from prompt_toolkit.document import Document  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from validator import shell  # noqa: E402


def _reader(res=None):
    sh = shell.Shell()
    sh.res = res
    with create_pipe_input() as inp:
        read = shell._make_reader(sh, pt_input=inp, pt_output=DummyOutput())
    return sh, read.session


def _complete(session, text):
    return [c.text for c in session.completer.get_completions(Document(text, len(text)), None)]


def test_commands_complete_with_descriptions():
    _, s = _reader()
    assert "/findings" in _complete(s, "/fi")
    assert _complete(s, "/q") == ["/questions"]
    assert {"крит", "сущ", "незн"} == set(_complete(s, "/findings "))
    assert "ieee830" in _complete(s, "/standards iso29148,ie")


def test_paths_complete_documents_only():
    _, s = _reader()
    got = _complete(s, "samples/requirements.")
    assert "pdf" in got and "md" in got
    assert not any(x.endswith("py") for x in _complete(s, "validator/c"))


def test_ids_and_example_questions_after_a_check(tmp_path):
    from tests.test_pipeline_fake import SAMPLE, FakeClient
    from validator import pipeline
    from validator.config import Config

    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False)
    res = pipeline.run(str(SAMPLE), cfg, log=lambda *_: None, client_factory=lambda red, audit: FakeClient(red))
    sh, s = _reader(res)
    assert "Н-01" in _complete(s, "н-0")
    assert "В-1" in _complete(s, "В")
    assert "почему нельзя начинать разработку?" in _complete(s, "почему")
    b = Buffer()
    b.text = "/hist"
    assert s.auto_suggest.get_suggestion(b, b.document).text == "ory"


def test_model_suggestions_per_provider(tmp_path, monkeypatch):
    from validator import config

    monkeypatch.setattr(config, "env_path", lambda: tmp_path / ".env")  # настоящий .env не трогаем
    sh, s = _reader()
    sh.cfg.provider = "anthropic"
    got = _complete(s, "/model cl")
    assert got[:3] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    assert "claude-sonnet-5-5" in _complete(s, "/model sonnet")
    sh.cfg.provider = "openai"
    assert {"gpt-5-mini", "gpt-5", "qwen3:8b"} <= set(_complete(s, "/model "))
    sh._live_models = {"openai": ["deepseek-chat"]}
    assert "deepseek-chat" in _complete(s, "/model deep")
    shell._model(sh, "2")  # выбор по номеру
    assert sh.cfg.model == "gpt-5" and "OPENAI_MODEL=gpt-5" in (tmp_path / ".env").read_text(encoding="utf-8")
