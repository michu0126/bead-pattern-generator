import asyncio
import base64
from io import BytesIO

import pytest
from PIL import Image

from app.ai import AIServiceError, ai_configured, create_pattern_reference, generate_direct_bead_pattern, image_model, remove_background, suggest_cutout_subjects, vision_model
from app.api_logs import list_api_calls


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("API_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("API_CALL_LOG_FILE", str(tmp_path / "api-calls.jsonl"))


def test_ai_is_optional(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    assert ai_configured() is False
    assert image_model() == "gpt-image-2"
    assert vision_model() == "gpt-5.5"


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
    log = list_api_calls()[0]
    assert log["dispatched"] is True
    assert log["success"] is True
    assert log["model"] == "gpt-image-2"
    assert log["endpoint"] == "https://api.openai.com/v1/images/edits"
    assert "api_key" not in log
    assert "image" not in log


def test_ai_call_requires_server_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AIServiceError, match="OpenAI 兼容 API"):
        asyncio.run(remove_background(b"input", "image.png", "image/png", "subject"))



def test_vision_analysis_uses_chat_completions_and_returns_choices(monkeypatch):
    class FakeResponse:
        status_code = 200
        headers = {"x-request-id": "vision-test"}
        def json(self):
            return {"choices": [{"message": {"content": '{"subjects":[{"label":"小猫","prompt":"the small cat"}]}'}}]}

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, *, headers, json):
            assert url == "https://api.openai.com/v1/chat/completions"
            assert json["model"] == "gpt-5.5"
            assert json["messages"][0]["content"][1]["type"] == "image_url"
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setattr("app.ai.httpx.AsyncClient", FakeClient)
    choices = asyncio.run(suggest_cutout_subjects(b"image", "image/png", ""))
    assert choices == [{"id": "subject-1", "label": "小猫", "prompt": "the small cat"}]
    log = list_api_calls()[0]
    assert log["operation"] == "vision_subject_analysis"
    assert log["model"] == "gpt-5.5"


def test_opaque_image_edit_response_is_rejected_and_logged(monkeypatch):
    original_buffer = BytesIO()
    Image.new("RGB", (20, 20), (12, 34, 56)).save(original_buffer, format="PNG")
    original = original_buffer.getvalue()
    opaque_buffer = BytesIO()
    Image.new("RGBA", (20, 20), (200, 100, 50, 255)).save(opaque_buffer, format="PNG")
    opaque = opaque_buffer.getvalue()

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(opaque).decode("ascii")}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, data, files):
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setattr("app.ai.httpx.AsyncClient", FakeClient)
    with pytest.raises(AIServiceError, match="几乎没有透明背景"):
        asyncio.run(remove_background(original, "person.png", "image/png", "woman"))

    log = list_api_calls()[0]
    assert log["success"] is False
    assert log["response"]["alpha"]["transparent_percent"] == 0.0
    assert "background=transparent" in log["error"]


def test_image2_pattern_reference_preserves_source_alpha_and_logs(monkeypatch):
    source_buffer = BytesIO()
    source = Image.new("RGBA", (2, 1), (10, 20, 30, 255))
    source.putpixel((0, 0), (10, 20, 30, 0))
    source.save(source_buffer, format="PNG")
    generated_buffer = BytesIO()
    Image.new("RGB", (2, 1), (200, 100, 50)).save(generated_buffer, format="PNG")

    class FakeResponse:
        status_code = 200
        headers = {"x-request-id": "pattern-reference-test"}

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(generated_buffer.getvalue()).decode("ascii")}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, data, files):
            assert url == "https://api.openai.com/v1/images/edits"
            assert data["model"] == "gpt-image-2"
            assert data["background"] == "transparent"
            assert "do not draw a grid" in data["prompt"].lower()
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setattr("app.ai.httpx.AsyncClient", FakeClient)
    result = asyncio.run(create_pattern_reference(source_buffer.getvalue(), "source.png", "image/png"))
    output = Image.open(BytesIO(result)).convert("RGBA")
    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((1, 0)) == (200, 100, 50, 255)
    log = list_api_calls()[0]
    assert log["operation"] == "image_pattern_reference"
    assert log["success"] is True


def test_image2_direct_pattern_request_includes_board_and_mard_palette(monkeypatch):
    chart_buffer = BytesIO()
    Image.new("RGB", (64, 64), (250, 250, 250)).save(chart_buffer, format="PNG")

    class FakeResponse:
        status_code = 200
        headers = {}

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(chart_buffer.getvalue()).decode("ascii")}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, data, files):
            assert data["model"] == "gpt-image-2"
            assert "exactly 52 columns and 52 rows" in data["prompt"]
            assert "H7=#000000" in data["prompt"]
            assert data["background"] == "opaque"
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setattr("app.ai.httpx.AsyncClient", FakeClient)
    result = asyncio.run(generate_direct_bead_pattern(b"source", "subject.png", "image/png", 52, 52))
    assert result == chart_buffer.getvalue()
    log = list_api_calls()[0]
    assert log["operation"] == "image_direct_pattern_generation"
    assert log["input"]["board"] == "52x52"
