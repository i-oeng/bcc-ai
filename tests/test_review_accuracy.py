from pathlib import Path

import pytest

from eval.score import adjudicated
from validator import pipeline, report, structured
from validator.config import Config
from validator.grounding import Grounder
from validator.loader import load
from validator.schemas import AcceptanceCheck, ConsistencyReview, RequirementBatch, Verification
from tests.test_pipeline_fake import FakeClient


def test_pdf_month_boundary_has_two_sources(tmp_path):
    sample = Path(__file__).resolve().parent.parent / "samples" / "requirements.pdf"
    doc = load(sample)
    extracted, residual = structured.extract(Grounder(doc))
    assert len(extracted) == 7 and residual.count("\n") == doc.text.count("\n")
    assert "Февральский счётчик вырос" not in residual

    class MissingReviews(FakeClient):
        def generate(self, name, system, prompt, schema):
            if schema is ConsistencyReview:
                example = next(ln.split()[0] for ln in prompt.splitlines() if "Февральский счётчик вырос" in ln
                               and ln.startswith("ТР-") and "| строка " in ln)
                return ConsistencyReview(verdicts=[], findings=[], acceptance_checks=[
                    AcceptanceCheck(acceptance_id=example, req_id="ТР-02", status="agrees",
                                    expected="Февраль", stated="Февраль", question="")
                ])
            if schema is RequirementBatch:
                self.prompts.append((name, prompt))
                return RequirementBatch(reviews=[])
            if schema is Verification:
                self.prompts.append((name, prompt))
                return Verification(items=[])
            return super().generate(name, system, prompt, schema)

    clients = []

    def factory(redactor, _):
        clients.append(MissingReviews(redactor))
        return clients[0]

    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False)
    res = pipeline.run(str(sample), cfg, log=lambda *_: None, client_factory=factory)
    check = next(c for c in res.acceptance_checks if c.status == "conflict")
    assert "31 января" in check.expected and "Февральский" in check.stated
    finding, = res.open_findings()
    assert [a.text for a in finding.anchors] == [res.req(check.req_id).quote, res.req(check.acceptance_id).quote]
    assert any(f.verification_status == "unreviewed" for f in res.findings)
    assert sum(name == "verify_missing" for name, _ in clients[0].prompts) == 1
    assert any(name == "review_1_missing" for name, _ in clients[0].prompts)
    assert res.questions[0].finding_ids == [finding.fid]
    assert res.decision_dev.startswith("НЕЛЬЗЯ")
    text = report.Renderer(res).render()
    assert "Сверка критериев приёмки" in text and "противоречие" in text and "? не проверено" in text
    assert res.phase_seconds.keys() >= {"segment", "review", "verify_and_dedupe", "final"}
    res.findings.clear()
    pipeline._decide(res)
    assert res.decision_dev.startswith("ГОТОВНОСТЬ НЕ ПОДТВЕРЖДЕНА")


def test_score_credits_reviewed_claims_and_not_duplicate_findings(capsys):
    defects = [{"id": "G01", "core": True}, {"id": "G02", "core": True}]
    findings = {"Н-01": {"severity": "critical"}, "Н-02": {"severity": "major"}}
    review = {"defects": {"G01": {"finding_ids": ["Н-01"], "credit": "full"},
                          "G02": {"finding_ids": ["Н-02"], "credit": "partial"}},
              "blockers": {"Н-01": True}}
    adjudicated(defects, findings, review)
    output = capsys.readouterr().out
    assert "полнота 1/2 (50%)" in output and "с учётом частичных 75%" in output
    assert "Точность блокеров: 1/1 (100%)" in output
    review["defects"]["G01"]["finding_ids"] = ["Н-03"]
    with pytest.raises(ValueError, match="подтверждённых уникальных"):
        adjudicated(defects, findings, review)
