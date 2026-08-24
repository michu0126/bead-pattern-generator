from fastapi.testclient import TestClient

from app.api_logs import clear_api_calls, list_api_calls, record_api_call
from app.main import app


client = TestClient(app)


def test_api_logs_are_persistent_bounded_and_sanitized(monkeypatch, tmp_path):
    monkeypatch.setenv("API_CALL_LOG_FILE", str(tmp_path / "api-calls.jsonl"))
    monkeypatch.setenv("API_CALL_LOG_MAX_ENTRIES", "20")
    for index in range(25):
        assert record_api_call({
            "id": str(index),
            "success": True,
            "api_key": "must-not-be-saved",
            "authorization": "Bearer secret",
            "image": "raw-image",
            "b64_json": "encoded-image",
        })

    logs = list_api_calls(100)
    assert len(logs) == 20
    assert logs[0]["id"] == "24"
    assert logs[-1]["id"] == "5"
    assert all("api_key" not in entry for entry in logs)
    assert all("authorization" not in entry for entry in logs)
    assert all("image" not in entry for entry in logs)
    assert all("b64_json" not in entry for entry in logs)

    clear_api_calls()
    assert list_api_calls() == []


def test_api_log_page_requires_settings_password(monkeypatch, tmp_path):
    monkeypatch.setenv("API_CALL_LOG_FILE", str(tmp_path / "api-calls.jsonl"))
    monkeypatch.setenv("SETTINGS_PASSWORD", "admin-secret")
    record_api_call({"id": "visible", "success": True})

    assert client.get("/api/settings/logs").status_code == 401
    response = client.get(
        "/api/settings/logs",
        headers={"X-Settings-Password": "admin-secret"},
    )
    assert response.status_code == 200
    assert response.json()["logs"][0]["id"] == "visible"

    response = client.delete(
        "/api/settings/logs",
        headers={"X-Settings-Password": "admin-secret"},
    )
    assert response.status_code == 200
    assert list_api_calls() == []
