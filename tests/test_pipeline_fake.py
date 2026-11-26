"""Сквозной прогон конвейера с поддельной моделью: проверяем склейку шагов и отчёт без сети."""

from pathlib import Path

from validator import pipeline, report
from validator.config import Config
from validator.llm import Usage
from validator.schemas import (
    AnswerReview,
    Clarification,
    CompletenessReview,
    Conclusion,
    Dedup,
    Final,
    FixedDocument,
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
        if schema in (SetReview, CompletenessReview):
            return schema.model_validate({"coverage": [{"req_id": "ТР-02", "acceptance_ids": ["ТР-03"], "missing_scenarios": ["возврат"]},
                                                       {"req_id": "ТР-01", "acceptance_ids": [], "missing_scenarios": ["граница 1 000"]}],
                                          "verdicts": [{"criterion": "set_consistent", "verdict": "violation", "comment": "есть противоречия"}],
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
        if schema is Dedup:
            a = next((ln.split()[0] for ln in prompt.splitlines() if "Ф-2 относит" in ln), None)
            b = next((ln.split()[0] for ln in prompt.splitlines() if "То же противоречие" in ln), None)
            groups = [{"keep": a, "duplicates": [b], "reason": "одно противоречие"}] if a and b else []
            return Dedup.model_validate({"groups": groups})
        if schema is Final:
            return Final.model_validate({
                "questions": [{"priority": "blocking", "addressee": "Бизнес-заказчик", "req_ids": ["ТР-02"], "finding_ids": ["Н-01"],
                               "quote": "Месяц определяется по дате совершения покупки в Алматы", "question": "Какой месяц?",
                               "why": "Иначе тест не написать", "options": ["по дате совершения", "по дате проведения"]}],
                "development_rationale": "Есть противоречие Н-01.", "testing_rationale": "Нельзя.",
                "fix_first": [{"title": "Месяц", "finding_ids": ["Н-01"], "action": "Выбрать правило"}],
                "checklist": [{"item": "Требование может быть протестировано", "answer": "Нет",
                               "rationale": "Противоречие месяца", "finding_ids": ["Н-01"]}],
                "overall": "Документ сырой."})
        if schema is AnswerReview:
            fid, last = None, None
            for ln in prompt.splitlines():
                if ln.startswith("Н-"):
                    last = ln.split()[0]
                if "Ф-2 относит" in ln and fid is None:
                    fid = last
            assert "по дате совершения" in prompt  # ответ «1» развёрнут в текст варианта
            return AnswerReview.model_validate({"resolutions": [
                {"finding_id": fid, "status": "closed", "question_ids": ["В-1"], "severity": "critical",
                 "note": "Месяц по дате совершения; исправить критерий приёмки 2"}]})
        if schema is FixedDocument:
            return FixedDocument.model_validate({"markdown": "# Исправлено\n\nФ-2. Система должна относить покупку к месяцу по дате совершения.",
                                                 "changes": [{"finding_ids": ["Н-01"], "change": "Согласован месяц"}],
                                                 "open_points": ["[УТОЧНИТЬ: текст push]"]})
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


def test_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "DEDUPE_THRESHOLD", 1)
    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False)
    res = pipeline.run(str(SAMPLE), cfg, log=lambda *_: None, client_factory=lambda red, audit: FakeClient(red))
    assert [r.doc_id for r in res.requirements] == ["Ф-1", "Ф-2", "A. Критерий приёмки 2"]
    assert len(res.lost_requirements) == 1  # выдуманная цитата отброшена
    assert len(res.ungrounded) == 1  # находка без опоры на текст отброшена
    assert any(f.source == "rule" for f in res.findings)
    assert res.decision_dev.startswith("НЕЛЬЗЯ")
    # дубль объединён с основной находкой, номера подтверждённых сплошные
    main = next(f for f in res.findings if "Ф-2 относит" in f.problem)
    dup = next(f for f in res.findings if "То же противоречие" in f.problem and f.duplicate_of)
    assert dup.duplicate_of == main.fid and "ТР-03" in main.req_ids
    kept_ids = [f.fid for f in res.kept()]
    assert kept_ids == [f"Н-{i:02d}" for i in range(1, len(kept_ids) + 1)]
    out = report.write(res, tmp_path / "report.md")
    text = out.read_text(encoding="utf-8")
    for section in ["## 1. Паспорт проверки", "## 2. Как агент нарезал требования", "## 3. Проверка каждого требования",
                    "## 4. Проверка всего набора требований", "## 5. Вопросы для уточнения", "## 6. Чек-лист ревью требований",
                    "## 7. Сводка", "## 8. Итог", "## Кратко", "Покрытие функциональных требований"]:
        assert section in text, section
    assert "❌ нет" in text  # Ф-1 без критериев приёмки
    assert "строка" in text and "ТР-02" in text
    # состояние сохраняется, и отчёт перерисовывается без модели
    again = pipeline.load_state(res.audit_dir)
    assert report.Renderer(again).render() == text


def test_final_step_failure_still_writes_report(tmp_path):
    from validator.llm import LLMError

    class Failing(FakeClient):
        def generate(self, name, system, prompt, schema):
            if schema is Final:
                raise LLMError("503 перегрузка")
            return super().generate(name, system, prompt, schema)

    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False)
    res = pipeline.run(str(SAMPLE), cfg, log=lambda *_: None, client_factory=lambda red, audit: Failing(red))
    assert res.warnings and res.conclusion and res.conclusion.fix_first
    text = report.write(res, tmp_path / "r.md").read_text(encoding="utf-8")
    assert "Предупреждение" in text and "## 8. Итог" in text


def test_answers_loop_and_fixed_document(tmp_path, monkeypatch):
    """Прогон, файл вопросов, ответ заказчика, повторный прогон: дефект закрыт, итог пересчитан."""
    monkeypatch.setattr(pipeline, "DEDUPE_THRESHOLD", 1)
    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False)
    first = pipeline.run(str(SAMPLE), cfg, log=lambda *_: None, client_factory=lambda red, audit: FakeClient(red))
    rp = report.write(first, tmp_path / "report.md")
    qfile = report.write_extras(first, rp)["Вопросы заказчику (впишите ответы)"]
    text = qfile.read_text(encoding="utf-8")
    assert "## В-1. Какой месяц?" in text and "Ответ: " in text
    qfile.write_text(text.replace("Ответ: ", "Ответ: 1", 1), encoding="utf-8")

    second = pipeline.run(str(SAMPLE), cfg, log=lambda *_: None, client_factory=lambda red, audit: FakeClient(red),
                          answers_path=str(qfile), fix=True)
    assert second.answers and second.answers[0].answer.startswith("Вариант 1: по дате совершения")
    closed = [f for f in second.kept() if f.resolution == "closed"]
    assert len(closed) == 1 and closed[0] not in second.open_findings()
    out = report.write(second, tmp_path / "report2.md")
    extras = report.write_extras(second, out)
    body = out.read_text(encoding="utf-8")
    assert "Закрыто ответом заказчика" in body and "Учтённые ответы заказчика" in body and "Приложение D" in body
    assert extras["Исправленная редакция документа"].read_text(encoding="utf-8").startswith("# Исправлено")


def test_standards_change_prompts_and_report(tmp_path):
    """EARS добавляет критерий в промпт проверки и колонку в матрицу; без IEEE 830 нет ранжирования."""
    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False, standards=["ears"])
    fake = {}
    res = pipeline.run(str(SAMPLE), cfg, log=lambda *_: None,
                       client_factory=lambda red, audit: fake.setdefault("c", FakeClient(red)))
    review = next(p for n, p in fake["c"].prompts if n.startswith("review"))
    assert "ears_pattern" in review and "из 10 критериев" in review
    assert res.prof.ids == ["iso29148", "ears"]
    assert all(f.criterion not in ("set_ranked", "set_modifiable") for f in res.findings)
    text = report.write(res, tmp_path / "r.md").read_text(encoding="utf-8")
    assert "| EARS |" in text and "EARS (Easy Approach" in text


def test_stop_between_steps(tmp_path):
    import pytest

    from validator.llm import Cancelled

    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2  # после нарезки

    cfg = Config.load(runs_dir=str(tmp_path / "runs"), redact_ner=False)
    with pytest.raises(Cancelled):
        pipeline.run(str(SAMPLE), cfg, log=lambda *_: None, client_factory=lambda red, audit: FakeClient(red),
                     should_stop=stop)
