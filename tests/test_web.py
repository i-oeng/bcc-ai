"""Веб-API без сети: документы, маскирование, проверка с поддельной моделью, ответы, история, сравнение."""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_pipeline_fake import FakeClient
from web import api, services
from web.jobs import JobRegistry

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VALIDATOR_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("REDACT_NER", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(services, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(services, "WEB_REPORTS", tmp_path / "web_reports")
    monkeypatch.setattr(api, "REGISTRY", JobRegistry(client_factory=lambda red, audit: FakeClient(red)))
    return TestClient(api.app)


def _wait(client, jid):
    for _ in range(600):
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] != "running":
            assert j["status"] == "done", j
            return j
        time.sleep(0.1)
    raise AssertionError("проверка не закончилась")


def test_status_samples_and_frontend(client):
    st = client.get("/api/status").json()
    assert st["has_env_key"] and "api_key" not in st["defaults"]
    assert {"id": "sample:requirements.md", "name": "requirements.md"} in client.get("/api/samples").json()
    assert "Агент проверки требований" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_documents_are_ids_not_paths(client):
    assert client.get("/api/documents/sample:requirements.md").json()["lines"] > 0
    for bad in ["sample:..%2F.env", "upload:x.md", "file:C:/Windows/win.ini", "sample:"]:
        assert client.get(f"/api/documents/{bad}").status_code == 404, bad
    r = client.post("/api/documents", files={"file": ("my doc.md", "# Т\n\nСистема должна работать.\n".encode())})
    assert r.status_code == 200 and r.json()["id"].startswith("upload:")
    assert client.post("/api/documents", files={"file": ("x.exe", b"MZ")}).status_code == 400


def test_preview_masks_locally(client):
    p = client.post("/api/preview", json={"document": "sample:requirements_with_pii.md"}).json()
    assert p["replacements"] and "[КАРТА_1]" in p["masked"]
    assert all(len(r["value"]) <= 5 for r in p["replacements"])


def test_full_loop(client):
    j = _wait(client, client.post("/api/jobs", json={"document": "sample:requirements.md"}).json()["id"])
    assert any("Нарезка" in ln for ln in j["log"])
    rid = j["run_id"]
    view = client.get(f"/api/runs/{rid}").json()
    assert view["summary"]["requirements"] > 0 and view["findings"] and view["questions"]
    assert "<table>" in client.get(f"/api/runs/{rid}/report").text
    assert client.get(f"/api/runs/{rid}/report?format=md").text.startswith("#")
    assert client.get(f"/api/runs/{rid}/files/report").status_code == 200
    assert client.get(f"/api/runs/{rid}/files/fixed").status_code == 404

    assert client.post(f"/api/runs/{rid}/answers", json={"answers": {"В-1": " "}}).status_code == 400
    j2 = _wait(client, client.post(f"/api/runs/{rid}/answers",
                                   json={"answers": {"В-1": "Вариант 1: по дате совершения"}}).json()["id"])
    v2 = client.get(f"/api/runs/{j2['run_id']}").json()
    assert v2["summary"]["closed"] == 1 and v2["has_fixed"] and v2["answers"][0]["id"] == "В-1"
    assert client.get(f"/api/runs/{j2['run_id']}/files/fixed").status_code == 200

    runs = client.get("/api/runs").json()
    assert [r["id"] for r in runs] == [j2["run_id"], rid] and runs[0]["answered"]
    c = client.get(f"/api/compare?before={rid}&after={j2['run_id']}").json()
    assert c["after"]["open"] < c["before"]["open"]
    assert client.get("/api/runs/..%2F..%2Fsecret").status_code == 404


def test_parallel_jobs_queue_and_stop(client, monkeypatch):
    """Две проверки идут одновременно, третья ждёт в очереди; остановка снимает её до начала."""
    gate = threading.Event()

    class SlowClient(FakeClient):
        def generate(self, *a, **kw):
            gate.wait(5)
            return super().generate(*a, **kw)

    reg = JobRegistry(client_factory=lambda red, audit: SlowClient(red), max_parallel=2)
    monkeypatch.setattr(api, "REGISTRY", reg)
    ids = [client.post("/api/jobs", json={"document": "sample:requirements.md"}).json()["id"] for _ in range(3)]
    time.sleep(0.5)
    statuses = [client.get(f"/api/jobs/{i}").json()["status"] for i in ids]
    assert statuses.count("running") == 2 and statuses.count("queued") == 1
    queued = ids[statuses.index("queued")]
    assert client.post(f"/api/jobs/{queued}/stop").json()["stop_requested"]
    running = [i for i in ids if i != queued]
    client.post(f"/api/jobs/{running[0]}/stop")
    gate.set()
    for i in running[1:]:
        _wait(client, i)
    for i in (queued, running[0]):
        for _ in range(100):
            if client.get(f"/api/jobs/{i}").json()["status"] != "running":
                break
            time.sleep(0.1)
        assert client.get(f"/api/jobs/{i}").json()["status"] == "cancelled"
    assert len(client.get("/api/jobs").json()) == 3


def test_key_required_per_provider(client, monkeypatch):
    monkeypatch.setattr(services, "has_env_key", lambda provider=None: provider == "gemini")
    doc = {"document": "sample:requirements.md"}
    assert client.post("/api/jobs", json={**doc, "options": {"provider": "anthropic"}}).status_code == 400
    r = client.post("/api/jobs", json={**doc, "options": {"provider": "anthropic"}}, headers={"X-Api-Key": "sk-test"})
    assert r.status_code == 202 and r.json()["provider"] == "anthropic"
    assert client.post("/api/jobs", json={**doc, "options": {"standards": ["nope"]}}).status_code == 400


def test_status_lists_providers_and_standards(client):
    st = client.get("/api/status").json()
    assert {p["id"] for p in st["providers"]} == {"gemini", "openai", "anthropic"}
    ids = [s["id"] for s in st["standards"]]
    assert ids[0] == "iso29148" and {"ieee830", "ears", "gost34602"} <= set(ids)
    j = _wait(client, client.post("/api/jobs", json={"document": "sample:requirements.md",
                                                     "options": {"standards": ["ears"]}}).json()["id"])
    view = client.get(f"/api/runs/{j['run_id']}").json()
    assert [s["id"] for s in view["standards"]] == ["iso29148", "ears"]
    assert any(c["code"] == "ears_pattern" for c in view["criteria"])


def test_assistant_masks_question_and_restores_answer(client, monkeypatch):
    import validator.llm as llm

    sent = {}

    class Spy(FakeClient):
        def generate(self, name, system, prompt, schema):
            sent["prompt"] = prompt
            return super().generate(name, system, prompt, schema)

    monkeypatch.setattr(llm, "make_client", lambda cfg, red, audit, log=None, should_stop=None: Spy(red))
    rid = _wait(client, client.post("/api/jobs", json={"document": "sample:requirements.md"}).json()["id"])["run_id"]
    fid = client.get(f"/api/runs/{rid}").json()["findings"][0]["id"]
    r = client.post(f"/api/runs/{rid}/assistant", json={
        "question": "Что если клиент платит картой 4111 1111 1111 1111?", "focus": {"type": "finding", "id": fid},
        "history": [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "Здравствуйте"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "4111 1111 1111 1111" not in sent["prompt"] and "[КАРТА_1]" in sent["prompt"]
    assert fid in sent["prompt"] and "Пользователь: привет" in sent["prompt"]
    assert body["answer"] == "Строка 2: пример 4111 1111 1111 1111"  # значение возвращено, тире заменено
    assert body["suggested_text"].startswith("Система должна")
