from fastapi.testclient import TestClient

from app.main import app
from app.settings import load_settings


client = TestClient(app)


def test_settings_api_requires_container_password(monkeypatch, tmp_path):
    monkeypatch.setenv("API_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("SETTINGS_PASSWORD", "admin-secret")
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers={"X-Settings-Password": "wrong"}).status_code == 401


def test_settings_api_saves_but_never_returns_key(monkeypatch, tmp_path):
    monkeypatch.setenv("API_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    monkeypatch.setenv("SETTINGS_PASSWORD", "admin-secret")
    headers = {"X-Settings-Password": "admin-secret"}
    payload = {
        "api_url": "https://provider.example/openai/v1/",
        "api_key": "sk-private-value",
        "model": "provider-image-model",
        "quality": "high",
    }
    response = client.put("/api/settings", headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["has_api_key"] is True
    assert "api_key" not in response.json()
    assert "sk-private-value" not in response.text

    response = client.get("/api/settings", headers=headers)
    assert response.status_code == 200
    assert response.json()["api_url"] == "https://provider.example/openai/v1"
    assert "api_key" not in response.json()

    second_payload = {
        "api_url": "https://second-provider.example/v1",
        "api_key": "sk-replacement-value",
        "model": "gpt-image-2",
        "quality": "low",
    }
    second_response = client.put("/api/settings", headers=headers, json=second_payload)
    assert second_response.status_code == 200
    assert second_response.json()["message"] == "API 设置已保存并校验"
    saved = load_settings()
    assert saved.api_url == "https://second-provider.example/v1"
    assert saved.api_key == "sk-replacement-value"
    assert saved.model == "gpt-image-2"
    assert saved.quality == "low"

    second_payload.pop("api_key")
    second_payload["model"] = "replacement-model"
    assert client.put("/api/settings", headers=headers, json=second_payload).status_code == 200
    assert load_settings().api_key == "sk-replacement-value"

    response = client.delete("/api/settings/key", headers=headers)
    assert response.status_code == 200
    assert response.json()["has_api_key"] is False
    assert load_settings().api_key == ""


def test_board_api_exposes_common_pegboard_sizes():
    response = client.get("/api/boards")
    assert response.status_code == 200
    boards = response.json()
    assert [(item["width"], item["height"]) for item in boards] == [
        (52, 52),
        (72, 72),
        (78, 78),
        (104, 104),
    ]
    assert [item["id"] for item in boards] == ["52x52", "72x72", "78x78", "104x104"]
