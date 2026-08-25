from __future__ import annotations

import base64
import json
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


def vision_model() -> str:
    return load_settings().vision_model



def _chat_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message", {})
    return str(message.get("content", "")).strip() if isinstance(message, dict) else ""


def _subject_options(text: str, requested: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(text.strip().strip(chr(96)).removeprefix("json").strip())
    except json.JSONDecodeError:
        parsed = {}
    raw = parsed.get("subjects", []) if isinstance(parsed, dict) else []
    options = []
    for item in raw[:4] if isinstance(raw, list) else []:
        if isinstance(item, dict) and str(item.get("label", "")).strip():
            label = str(item["label"]).strip()[:80]
            options.append({"id": f"subject-{len(options)+1}", "label": label, "prompt": str(item.get("prompt", label)).strip()[:300] or label})
    fallback = requested.strip() or "图片中的主要前景主体"
    return options or [{"id": "subject-1", "label": fallback[:80], "prompt": fallback[:300]}]


async def suggest_cutout_subjects(image: bytes, content_type: str, requested: str) -> list[dict[str, str]]:
    try:
        settings = load_settings()
    except SettingsError as error:
        raise AIServiceError(str(error)) from error
    if not settings.vision_enabled:
        raise AIServiceError("服务器尚未配置可用的识图模型")
    prompt = (
        "Analyze this image for a later background-removal step. Return JSON only: "
        "{\"subjects\":[{\"label\":\"short Chinese label\",\"prompt\":\"precise English subject description\"}]}. "
        "List up to 4 visible foreground subjects; do not edit or generate an image. "
        + (f"Prioritize this requested subject: {requested.strip()}." if requested.strip() else "Put the main subject first.")
    )
    data_url = f"data:{content_type or 'image/png'};base64,{base64.b64encode(image).decode('ascii')}"
    payload = {"model": settings.vision_model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
    ]}], "temperature": 0}
    call_id, dispatched, response = str(uuid4()), False, None
    started_at, clock = datetime.now(timezone.utc).isoformat(timespec="milliseconds"), time.monotonic()

    def log(success: bool, error: str | None = None) -> None:
        headers = getattr(response, "headers", {}) if response is not None else {}
        record_api_call({"id": call_id, "started_at": started_at, "operation": "vision_subject_analysis",
            "provider": "OpenAI compatible", "endpoint": settings.chat_url, "model": settings.vision_model,
            "dispatched": dispatched, "success": success, "duration_ms": round((time.monotonic()-clock)*1000),
            "input": {"bytes": len(image), "content_type": content_type or "image/png", "subject_provided": bool(requested.strip())},
            "request": {"response_format": "json-instruction", "image_detail": "high"},
            "response": {"status_code": getattr(response, "status_code", None), "content_type": headers.get("content-type"), "request_id": headers.get("x-request-id") or headers.get("request-id")},
            "error": error})

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0), follow_redirects=False) as client:
            dispatched = True
            response = await client.post(settings.chat_url, headers={"Authorization": f"Bearer {settings.api_key}"}, json=payload)
    except httpx.RequestError as error:
        log(False, f"网络错误：{type(error).__name__}")
        raise AIServiceError("无法连接识图 API，请检查接口地址和群晖网络") from error
    if response.status_code >= 400:
        try: detail = str(response.json().get("error", {}).get("message") or "")[:300]
        except ValueError: detail = ""
        detail = detail or f"识图 API 请求失败（{response.status_code}）"
        log(False, detail)
        raise AIServiceError(detail)
    try: options = _subject_options(_chat_text(response.json()), requested)
    except ValueError as error:
        log(False, "识图模型没有返回兼容的文本结果")
        raise AIServiceError("识图模型没有返回兼容的文本结果") from error
    log(True)
    return options


MIN_TRANSPARENT_FRACTION = 0.005


def _apply_ai_alpha_to_original(original_bytes: bytes, edited_bytes: bytes) -> tuple[bytes, dict[str, float | int]]:
    """Apply only a verified transparent mask, never AI-generated colour pixels."""
    original = ImageOps.exif_transpose(Image.open(BytesIO(original_bytes))).convert("RGBA")
    edited = Image.open(BytesIO(edited_bytes)).convert("RGBA")
    alpha = edited.getchannel("A")
    if alpha.size != original.size:
        alpha = alpha.resize(original.size, Image.Resampling.LANCZOS)

    histogram = alpha.histogram()
    total_pixels = alpha.width * alpha.height
    transparent_pixels = sum(histogram[:250])
    transparent_fraction = transparent_pixels / max(total_pixels, 1)
    alpha_info: dict[str, float | int] = {
        "transparent_pixels": transparent_pixels,
        "total_pixels": total_pixels,
        "transparent_percent": round(transparent_fraction * 100, 2),
        "alpha_min": next((value for value, count in enumerate(histogram) if count), 255),
        "alpha_max": next((value for value in range(255, -1, -1) if histogram[value]), 255),
    }
    if transparent_fraction < MIN_TRANSPARENT_FRACTION:
        raise AIServiceError(
            "图像编辑接口返回的 PNG 几乎没有透明背景（透明像素 "
            f"{alpha_info['transparent_percent']}%）。该兼容接口可能忽略了 background=transparent；"
            "请改用支持透明背景的图像编辑模型或选择本地识图。"
        )

    original.putalpha(alpha)
    output = BytesIO()
    original.save(output, format="PNG", optimize=True)
    return output.getvalue(), alpha_info


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

    def log_call(
        success: bool,
        *,
        response=None,
        error: str | None = None,
        output_bytes: int | None = None,
        alpha_info: dict[str, float | int] | None = None,
    ) -> None:
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
                "alpha": alpha_info,
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
        result, alpha_info = _apply_ai_alpha_to_original(image, edited)
        log_call(True, response=response, output_bytes=len(result), alpha_info=alpha_info)
        return result
    except AIServiceError as error:
        log_call(False, response=response, error=str(error))
        raise
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        log_call(False, response=response, error="API 没有返回兼容的 b64_json 图片")
        raise AIServiceError("API 没有返回兼容的 b64_json 图片") from error
