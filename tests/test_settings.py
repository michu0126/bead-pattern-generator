import asyncio

import pytest

from app.settings import (
    AISettings,
    SettingsAuthError,
    chat_completion_url,
    image_edit_url,
    load_settings,
    models_url,
    normalize_api_url,
    public_settings,
    require_settings_password,
    save_settings,
    test_api_connection as check_api_connection,
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("API_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_openai_compatible_urls_accept_base_or_full_endpoint():
    assert normalize_api_url("https://api.openai.com/v1/") == "https://api.openai.com/v1"
    assert image_edit_url("https://api.openai.com/v1") == "https://api.openai.com/v1/images/edits"
    assert image_edit_url("http://nas.local:11434/v1/images/edits") == "http://nas.local:11434/v1/images/edits"
    assert models_url("http://nas.local:11434/v1/images/edits") == "http://nas.local:11434/v1/models"
    assert chat_completion_url("http://nas.local:11434/v1/images/edits") == "http://nas.local:11434/v1/chat/completions"


@pytest.mark.parametrize("url", ["", "ftp://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?q=secret"])
def test_invalid_api_urls_are_rejected(url):
    with pytest.raises(RuntimeError):
        normalize_api_url(url)


def test_settings_roundtrip_never_exposes_key():
    expected = AISettings("https://provider.example/v1", "sk-private", "image-model", "gpt-5.5", "high")
    save_settings(expected)
    assert load_settings() == expected
    visible = public_settings(load_settings())
    assert visible["has_api_key"] is True
    assert "api_key" not in visible
    assert "sk-private" not in str(visible)


def test_settings_password_is_required_and_compared(monkeypatch):
    monkeypatch.delenv("SETTINGS_PASSWORD", raising=False)
    with pytest.raises(SettingsAuthError, match="尚未设置"):
        require_settings_password("anything")
    monkeypatch.setenv("SETTINGS_PASSWORD", "admin-secret")
    with pytest.raises(SettingsAuthError, match="不正确"):
        require_settings_password("wrong")
    require_settings_password("admin-secret")


def test_connection_uses_models_endpoint_and_bearer_key(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "image-model"}]}

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers):
            assert url == "https://provider.example/openai/v1/models"
            assert headers == {"Authorization": "Bearer sk-test"}
            return FakeResponse()

    monkeypatch.setattr("app.settings.httpx.AsyncClient", FakeClient)
    result = asyncio.run(check_api_connection(AISettings(
        "https://provider.example/openai/v1",
        "sk-test",
        "image-model",
        "gpt-5.5",
        "medium",
    )))
    assert result["ok"] is True
    assert "1 个模型" in result["message"]


def test_connection_returns_model_ids(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"id": "gpt-image-2"},
                    {"id": "gpt-5"},
                    {"id": "gpt-image-2"},
                    {"missing": "id"},
                ]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers):
            return FakeResponse()

    import asyncio
    from app.settings import AISettings, test_api_connection

    monkeypatch.setattr("app.settings.httpx.AsyncClient", FakeClient)
    result = asyncio.run(test_api_connection(AISettings(
        api_url="https://provider.example/v1",
        api_key="secret",
        model="gpt-image-2",
        vision_model="gpt-5.5",
        quality="high",
    )))
    assert result["models"] == ["gpt-5", "gpt-image-2"]


def test_legacy_gpt5_image_model_is_migrated_to_vision_model(tmp_path, monkeypatch):
    path = tmp_path / "data" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"api_url":"https://provider.example/v1","api_key":"sk-test","model":"gpt-5.5","quality":"medium"}', encoding="utf-8")
    monkeypatch.setenv("API_SETTINGS_FILE", str(path))
    settings = load_settings()
    assert settings.vision_model == "gpt-5.5"
    assert settings.model == "gpt-image-2"
