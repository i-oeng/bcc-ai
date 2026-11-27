"""Слияние одинаковых находок без модели и верификация параллельными частями."""

from validator import pipeline
from validator.config import Config
from validator.grounding import Anchor
from validator.schemas import Finding, Verification
from tests.test_pipeline_fake import SAMPLE, FakeClient


def _finding(fid, criterion, lines, problem, severity="critical"):
    anchors = [Anchor(text=f"line {ln}", line=ln, line_end=ln, section="", page=None, score=100) for ln in lines]
    f = Finding(fid, "set", criterion, severity, [], anchors, problem, "Исправить.", "llm")
    f.kept = True
    return f


def _merge(*findings):
    res = pipeline.RunResult.__new__(pipeline.RunResult)
    res.findings = list(findings)
    return pipeline._merge_same_findings(res)


def test_contradiction_between_same_lines_is_merged():
    a = _finding("Н-05", "correct", [41, 73], "По Ф-2 месяц по дате совершения, критерий приёмки считает по дате проведения.")
    b = _finding("Н-29", "set_consistent", [73, 41], "Пример относит покупку 31 января к февралю, хотя правило требует январь.")
    assert _merge(a, b) == 1
    assert b.duplicate_of == "Н-05" and not a.duplicate_of


def test_different_defects_on_same_lines_stay_separate():
    a = _finding("Н-01", "correct", [41, 73], "Противоречие месяца.")
    b = _finding("Н-02", "unambiguous", [41, 73], "Не ясно, какой часовой пояс у даты.")
    c = _finding("Н-03", "set_consistent", [41], "Противоречие с разделом данных.")
    assert _merge(a, b, c) == 0


def test_verification_runs_in_parallel_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "VERIFY_CHUNK", 2)
    names = []

    class Recording(FakeClient):
        def generate(self, name, system, prompt, schema):
            if schema is Verification:
                names.append(name)
                own = [ln.split()[0] for ln in prompt.splitlines() if ln.startswith("Н-")]
                assert own and len(own) <= 2
                assert "Остальные находки этой проверки" in prompt
                return Verification.model_validate({"items": [
                    {"finding_id": i, "keep": True, "severity": "", "duplicate_of": "", "reason": "ок"} for i in own]})
            return super().generate(name, system, prompt, schema)

    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False)
    res = pipeline.run(str(SAMPLE), cfg, log=lambda *_: None, client_factory=lambda red, audit: Recording(red))
    assert len(names) > 1 and all(n.startswith("verify_") for n in names)
    assert "verify_missing" not in names
    assert all(f.verification_status == "confirmed" for f in res.findings)
