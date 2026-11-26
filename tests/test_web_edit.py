"""Редактируемые стандарты, рабочая редакция документа и действия ИИ-помощника (без сети)."""

import re

import pytest

from tests.test_pipeline_fake import FakeClient
from tests.test_web import _wait, client  # noqa: F401  фикстура
from validator import criteria
from validator.schemas import AnswerDraft, AssistantReply, DocEdit


@pytest.fixture
def std_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VALIDATOR_STANDARDS_DIR", str(tmp_path / "custom_standards"))
    criteria.reload()
    yield tmp_path / "custom_standards"
    monkeypatch.delenv("VALIDATOR_STANDARDS_DIR")
    criteria.reload()


def test_edit_builtin_add_custom_reset(client, std_dir):  # noqa: F811
    kinds = {s["id"]: s["kind"] for s in client.get("/api/standards").json()}
    assert kinds["ears"] == "builtin"

    raw = client.get("/api/standards/ears").json()
    raw["criteria"][0]["name"] = "Шаблон EARS, строгий"
    r = client.put("/api/standards/ears", json=raw)
    assert r.status_code == 200 and r.json()["kind"] == "modified"
    assert criteria.profile(["ears"]).by_code["ears_pattern"].name == "Шаблон EARS, строгий"
    assert (std_dir / "ears.json").is_file()
    assert "Шаблон EARS" in (criteria.BUILTIN_DIR / "ears.json").read_text(encoding="utf-8")  # встроенный файл не тронут

    mine = {"id": "bank_rules", "title": "Правила банка", "criteria": [
        {"code": "money_units", "name": "Единицы сумм", "question": "Суммы указаны с валютой и точностью."},
        {"code": "set_audit", "scope": "set", "name": "Аудит", "question": "Есть требования к журналу операций."}]}
    r = client.put("/api/standards/bank_rules?create=true", json=mine)
    assert r.status_code == 200 and r.json()["kind"] == "custom"
    assert client.put("/api/standards/bank_rules?create=true", json=mine).status_code == 409
    p = criteria.profile(["bank_rules"])
    assert p.has("money_units") and [c.code for c in p.set][-1] == "set_audit"
    st = client.get("/api/status").json()["standards"]
    assert any(s["id"] == "bank_rules" for s in st)

    bad = {"id": "bad_one", "title": "Б", "criteria": [{"code": "coverage", "scope": "set", "name": "x", "question": "y"}]}
    r = client.put("/api/standards/bad_one", json=bad)
    assert r.status_code == 400 and "set_" in r.json()["detail"]
    assert client.put("/api/standards/other", json=mine).status_code == 400  # id в адресе и в теле разные
    assert not (std_dir / "bad_one.json").exists()

    assert client.delete("/api/standards/ears").json()["result"] == "reset"
    assert client.delete("/api/standards/bank_rules").json()["result"] == "deleted"
    assert client.delete("/api/standards/iso29148").status_code == 400
    assert {s["id"]: s["kind"] for s in client.get("/api/standards").json()}["ears"] == "builtin"


def test_draft_edits_undo_and_recheck(client):  # noqa: F811
    rid = _wait(client, client.post("/api/jobs", json={"document": "sample:requirements.md"}).json()["id"])["run_id"]
    d = client.get(f"/api/runs/{rid}/draft").json()
    assert not d["changed"] and not d["can_undo"]
    assert client.post(f"/api/runs/{rid}/draft/check", json={}).status_code == 400  # без правок нечего проверять

    line_no = next(i for i, ln in enumerate(d["text"].split("\n"), 1) if ln.startswith("Дать клиенту"))
    old = d["text"].split("\n")[line_no - 1]
    # модель могла скопировать номер строки и сбить пробелы: правка всё равно находит фрагмент
    messy = f"{line_no}| " + old.replace(" ", "  ", 2)
    d = client.post(f"/api/runs/{rid}/draft/edit", json={"old_text": messy, "new_text": "Система должна уведомлять клиента.",
                                                         "line": line_no, "reason": "Н-01"}).json()
    assert d["changed"] and "Система должна уведомлять клиента." in d["text"] and old not in d["text"]
    d = client.post(f"/api/runs/{rid}/draft/edit", json={"old_text": "", "new_text": "Новое требование.", "line": 1}).json()
    assert d["text"].split("\n")[1] == "Новое требование."
    assert client.post(f"/api/runs/{rid}/draft/edit", json={"old_text": "такого текста нет", "new_text": "x"}).status_code == 409

    d = client.post(f"/api/runs/{rid}/draft/undo").json()
    assert "Новое требование." not in d["text"] and "Система должна уведомлять клиента." in d["text"]
    d = client.put(f"/api/runs/{rid}/draft", json={"text": d["text"] + "\nДописано вручную.\n"}).json()
    assert [e["kind"] for e in d["edits"]] == ["edit", "manual"]
    assert client.get(f"/api/runs/{rid}/draft/file").text.endswith("Дописано вручную.\n")

    j = client.post(f"/api/runs/{rid}/draft/check", json={}).json()
    assert j["document"].endswith("_редакция.md")
    done = _wait(client, j["id"])
    assert done["progress"] == 1.0 and done["stage"] == "Готово"

    d = client.post(f"/api/runs/{rid}/draft/reset").json()
    assert not d["changed"] and d["can_undo"]  # возврат к исходному тоже отменяется


def test_assistant_proposes_edits_and_answers(client, monkeypatch):  # noqa: F811
    import validator.llm as llm

    sent = {}

    class Spy(FakeClient):
        def generate(self, name, system, prompt, schema):
            if schema is not AssistantReply:
                return super().generate(name, system, prompt, schema)
            sent["prompt"] = prompt
            doc = prompt.split("<document>")[1]
            num, text = next((n, t) for n, t in re.findall(r"(?m)^\s*(\d+)\| (.*\[КАРТА_\d+\].*)$", doc))
            return AssistantReply(answer="Готово — правка ниже", suggested_text="", edits=[
                DocEdit(line=int(num), old_text=text, new_text=text + " (уточнено)", reason="Н-01"),
                DocEdit(line=1, old_text="текста нет в документе", new_text="x", reason="")],
                answers=[AnswerDraft(question_id="В-1", answer="Вариант 1: по дате совершения", basis="строка 30"),
                         AnswerDraft(question_id="В-99", answer="лишний", basis="")])

    monkeypatch.setattr(llm, "make_client", lambda cfg, red, audit, log=None, should_stop=None: Spy(red))
    rid = _wait(client, client.post("/api/jobs", json={"document": "sample:requirements_with_pii.md"}).json()["id"])["run_id"]
    body = client.post(f"/api/runs/{rid}/assistant", json={"question": "Исправь требование с картой"}).json()

    assert "В-1. " in sent["prompt"] and "Вариант 1:" in sent["prompt"]
    e, missing = body["edits"]
    assert "[КАРТА_" not in e["old_text"] and e["ok"] and not missing["ok"]  # значение возвращено, фрагмент найден
    assert [a["question_id"] for a in body["answers"]] == ["В-1"]
    assert body["answer"] == "Готово: правка ниже"

    d = client.post(f"/api/runs/{rid}/draft/edit", json=e).json()
    assert e["old_text"] + " (уточнено)" in d["text"]
    # следующий вопрос помощник видит уже по рабочей редакции, замаскированной так же
    client.post(f"/api/runs/{rid}/assistant", json={"question": "Ещё раз"})
    assert "(уточнено)" in sent["prompt"] and not re.search(r"4\d{3} \d{4} \d{4} \d{4}", sent["prompt"])
