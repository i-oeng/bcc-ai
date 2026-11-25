"""Сквозной прогон конвейера с поддельной моделью: проверяем склейку шагов и отчёт без сети."""

from pathlib import Path

from validator import pipeline, report
from validator.config import Config
from validator.llm import Usage
from validator.schemas import (
    Clarification,
    Conclusion,
    RequirementBatch,
    Segmentation,
    SetReview,
    Verification,
)

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "requirements.md"


class FakeClient:
    def __init__(self, redactor):
        self.redactor = redactor
        self.usage = Usage()
        self.prompts = []

    def generate(self, name, system, prompt, schema):
        self.redactor.guard(system + prompt)
        self.usage.calls += 1
        self.prompts.append((name, prompt))
        if schema is Segmentation:
            return Segmentation.model_validate({
                "requirements": [
                    {"doc_id": "Ф-1", "section": "ФТ", "quote": "Держатель активной карты KZT задаёт целый порог от 1 000 до 1 000 000 ₸ включительно.", "summary": "Порог", "kind": "functional"},
                    {"doc_id": "Ф-2", "section": "ФТ", "quote": "Месяц определяется по дате совершения покупки в Алматы.", "summary": "Месяц", "kind": "functional"},
                    {"doc_id": "A. Критерий приёмки 2", "section": "АК", "quote": "Покупка 31 января в 23:58 на 4 000 ₸", "summary": "Переход месяца", "kind": "acceptance"},
                    {"doc_id": "X", "section": "?", "quote": "Система поддерживает оплату биткоином", "summary": "выдумка", "kind": "functional"},
                ],
                "skipped": [{"section": "Блок согласующих", "reason": "метаданные"}],
            })
        if schema is RequirementBatch:
            return RequirementBatch.model_validate({"reviews": [
                {"req_id": "ТР-02", "verdicts": [{"criterion": "correct", "verdict": "violation", "comment": "противоречит АК-2"}],
                 "findings": [{"criterion": "correct", "severity": "critical",
                               "quotes": ["Месяц определяется по дате совершения покупки в Алматы", "Февральский счётчик вырос на 4 000 ₸"],
                               "problem": "Ф-2 относит покупку к январю, критерий приёмки 2 к февралю.", "fix": "Выбрать одно правило."},
                              {"criterion": "verifiable", "severity": "major", "quotes": ["цитата, которой нет в документе вообще"],
                               "problem": "выдумка", "fix": "-"}]},
            ]})
        if schema is SetReview:
            return SetReview.model_validate({"verdicts": [{"criterion": "set_consistent", "verdict": "violation", "comment": "есть противоречия"}],
                                             "findings": [{"criterion": "set_consistent", "severity": "critical", "req_ids": ["ТР-02", "ТР-03"],
                                                           "quotes": ["Покупка 31 января в 23:58 на 4 000 ₸"], "problem": "То же противоречие месяца.", "fix": "Исправить."}]})
        if schema is Verification:
            ids = [ln.split()[0] for ln in prompt.splitlines() if ln.startswith("Н-")]
            items = []
            for i in ids:
                items.append({"finding_id": i, "keep": True, "severity": "", "duplicate_of": "", "reason": "ок"})
            if len(ids) > 1:
                items[-1]["keep"] = False
                items[-1]["reason"] = "домысел"
            return Verification.model_validate({"items": items})
        if schema is Clarification:
            return Clarification.model_validate({"questions": [
                {"priority": "blocking", "addressee": "Бизнес-заказчик", "req_ids": ["ТР-02"], "finding_ids": ["Н-01"],
                 "quote": "Месяц определяется по дате совершения покупки в Алматы", "question": "Какой месяц?",
                 "why": "Иначе тест не написать", "options": ["по дате совершения", "по дате проведения"]}]})
        if schema is Conclusion:
            return Conclusion.model_validate({"development_rationale": "Есть противоречие Н-01.", "testing_rationale": "Нельзя.",
                                              "fix_first": [{"title": "Месяц", "finding_ids": ["Н-01"], "action": "Выбрать правило"}],
                                              "overall": "Документ сырой."})
        raise AssertionError(schema)


def test_end_to_end(tmp_path):
    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False)
    res = pipeline.run(str(SAMPLE), cfg, log=lambda *_: None, client_factory=lambda red, audit: FakeClient(red))
    assert [r.doc_id for r in res.requirements] == ["Ф-1", "Ф-2", "A. Критерий приёмки 2"]
    assert len(res.lost_requirements) == 1  # выдуманная цитата отброшена
    assert len(res.ungrounded) == 1  # находка без опоры на текст отброшена
    assert any(f.source == "rule" for f in res.findings)
    assert res.decision_dev.startswith("НЕЛЬЗЯ")
    out = report.write(res, tmp_path / "report.md")
    text = out.read_text(encoding="utf-8")
    for section in ["## 1. Паспорт прогона", "## 2. Как агент нарезал требования", "## 3. Проверка каждого требования",
                    "## 4. Проверка всего набора требований", "## 5. Вопросы для уточнения", "## 6. Сводка", "## 7. Итог"]:
        assert section in text
    assert "строка" in text and "ТР-02" in text
