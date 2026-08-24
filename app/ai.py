from __future__ import annotations

import base64
from io import BytesIO

import httpx
from PIL import Image, ImageOps

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

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=20.0),
            follow_redirects=False,
        ) as client:
            response = await client.post(
                settings.edit_url,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                data=data,
                files=files,
            )
    except httpx.RequestError as error:
        raise AIServiceError("无法连接图像 API，请检查接口地址和群晖网络") from error

    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message")
        except ValueError:
            message = None
        detail = str(message)[:300] if message else f"图像 API 请求失败（{response.status_code}）"
        raise AIServiceError(detail)

    try:
        encoded = response.json()["data"][0]["b64_json"]
        edited = base64.b64decode(encoded, validate=True)
        return _apply_ai_alpha_to_original(image, edited)
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        raise AIServiceError("API 没有返回兼容的 b64_json 图片") from error
