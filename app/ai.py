from __future__ import annotations

import base64
from datetime import datetime, timezone
from io import BytesIO
import time
from uuid import uuid4

import httpx
from PIL import Image, ImageOps

from .api_logs import record_api_call
from .settings import SettingsError, load_settings


class AIServiceError(RuntimeError):
    pass


def ai_configured() -> bool:
    return load_settings().enabled


def image_model() -> str:
    return load_settings().model


def _apply_ai_alpha_to_original(original_bytes: bytes, edited_bytes: bytes) -> bytes:
    original = ImageOps.exif_transpose(Image.open(BytesIO(original_bytes))).convert("RGBA")
    edited = Image.open(BytesIO(edited_bytes)).convert("RGBA")
    alpha = edited.getchannel("A")
    if alpha.size != original.size:
        alpha = alpha.resize(original.size, Image.Resampling.LANCZOS)
    original.putalpha(alpha)
    output = BytesIO()
    original.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def remove_background(
    image: bytes,
    filename: str,
    content_type: str,
    subject: str,
) -> bytes:
    try:
        settings = load_settings()
    except SettingsError as error:
        raise AIServiceError(str(error)) from error
    if not settings.enabled:
        raise AIServiceError("服务器尚未配置可用的 OpenAI 兼容 API")

    subject_instruction = (
        f"Only keep this subject: {subject.strip()}." if subject.strip()
        else "Identify and keep the main foreground subject."
    )
    prompt = (
        f"{subject_instruction} Remove only the background and make it fully transparent. "
        "Preserve the subject's exact shape, pose, proportions, details, lighting, and original colors. "
        "Do not redraw, beautify, add, remove, recolor, or move any part of the subject. "
        "Return a PNG with transparency."
    )
    data = {
        "model": settings.model,
        "prompt": prompt,
        "background": "transparent",
        "output_format": "png",
        "quality": settings.quality,
        "size": "auto",
        "input_fidelity": "high",
    }
    files = {"image": (filename or "image.png", image, content_type or "image/png")}
    call_id = str(uuid4())
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    started_clock = time.monotonic()
    dispatched = False

    def log_call(success: bool, *, response=None, error: str | None = None, output_bytes: int | None = None) -> None:
        response_headers = getattr(response, "headers", {}) if response is not None else {}
        request_id = None
        for header_name in ("x-request-id", "openai-request-id", "request-id", "cf-ray"):
            if response_headers.get(header_name):
                request_id = response_headers[header_name]
                break
        record_api_call({
            "id": call_id,
            "started_at": started_at,
            "operation": "image_background_removal",
            "provider": "OpenAI compatible",
            "endpoint": settings.edit_url,
            "model": settings.model,
            "dispatched": dispatched,
            "success": success,
            "duration_ms": round((time.monotonic() - started_clock) * 1000),
            "input": {
                "bytes": len(image),
                "content_type": content_type or "image/png",
                "subject_provided": bool(subject.strip()),
            },
            "request": {
                "background": data["background"],
                "output_format": data["output_format"],
                "quality": data["quality"],
                "size": data["size"],
                "input_fidelity": data["input_fidelity"],
                "prompt_length": len(prompt),
            },
            "response": {
                "status_code": getattr(response, "status_code", None),
                "content_type": response_headers.get("content-type"),
                "request_id": request_id,
                "output_bytes": output_bytes,
            },
            "error": error,
        })

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=20.0),
            follow_redirects=False,
        ) as client:
            dispatched = True
            response = await client.post(
                settings.edit_url,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                data=data,
                files=files,
            )
    except httpx.RequestError as error:
        log_call(False, error=f"网络错误：{type(error).__name__}")
        raise AIServiceError("无法连接图像 API，请检查接口地址和群晖网络") from error

    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message")
        except ValueError:
            message = None
        detail = str(message)[:300] if message else f"图像 API 请求失败（{response.status_code}）"
        log_call(False, response=response, error=detail)
        raise AIServiceError(detail)

    try:
        encoded = response.json()["data"][0]["b64_json"]
        edited = base64.b64decode(encoded, validate=True)
        result = _apply_ai_alpha_to_original(image, edited)
        log_call(True, response=response, output_bytes=len(result))
        return result
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        log_call(False, response=response, error="API 没有返回兼容的 b64_json 图片")
        raise AIServiceError("API 没有返回兼容的 b64_json 图片") from error
