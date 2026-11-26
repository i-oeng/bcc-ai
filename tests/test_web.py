"""Веб-API без сети: документы, маскирование, прогон с поддельной моделью, ответы, история, сравнение."""

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
    raise AssertionError("прогон не закончился")


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


def test_one_job_at_a_time_and_key_required(client, monkeypatch):
    slow = JobRegistry(client_factory=lambda red, audit: (time.sleep(1), FakeClient(red))[1])
    monkeypatch.setattr(api, "REGISTRY", slow)
    assert client.post("/api/jobs", json={"document": "sample:requirements.md"}).status_code == 202
    assert client.post("/api/jobs", json={"document": "sample:requirements.md"}).status_code == 409
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(services, "has_env_key", lambda: False)
    assert client.post("/api/jobs", json={"document": "sample:requirements.md"}).status_code == 400
