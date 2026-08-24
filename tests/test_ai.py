import asyncio
import base64
from io import BytesIO

import pytest
from PIL import Image

from app.ai import AIServiceError, ai_configured, image_model, remove_background


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("API_SETTINGS_FILE", str(tmp_path / "settings.json"))


def test_ai_is_optional(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    assert ai_configured() is False
    assert image_model() == "gpt-image-2"


def test_ai_configuration_comes_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2-2026-04-21")
    assert ai_configured() is True
    assert image_model() == "gpt-image-2-2026-04-21"


def test_ai_call_is_server_side_and_returns_decoded_png(monkeypatch):
    original_buffer = BytesIO()
    Image.new("RGB", (2, 1), (12, 34, 56)).save(original_buffer, format="PNG")
    original = original_buffer.getvalue()
    edited_buffer = BytesIO()
    edited = Image.new("RGBA", (2, 1), (200, 100, 50, 255))
    edited.putpixel((0, 0), (200, 100, 50, 0))
    edited.save(edited_buffer, format="PNG")
    edited_result = edited_buffer.getvalue()

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(edited_result).decode("ascii")}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, data, files):
            assert url == "https://api.openai.com/v1/images/edits"
            assert headers["Authorization"] == "Bearer private-test-key"
            assert data["model"] == "gpt-image-2"
            assert data["background"] == "transparent"
            assert "cat" in data["prompt"]
            assert files["image"][1] == original
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.delenv("OPENAI_API_URL", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    monkeypatch.setattr("app.ai.httpx.AsyncClient", FakeClient)
    result = asyncio.run(remove_background(original, "cat.png", "image/png", "cat"))
    result_image = Image.open(BytesIO(result)).convert("RGBA")
    assert result_image.getpixel((0, 0)) == (12, 34, 56, 0)
    assert result_image.getpixel((1, 0)) == (12, 34, 56, 255)


def test_ai_call_requires_server_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AIServiceError, match="OpenAI 兼容 API"):
        asyncio.run(remove_background(b"input", "image.png", "image/png", "subject"))

