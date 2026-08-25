from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from io import BytesIO
import time
from uuid import uuid4

import httpx
from PIL import Image, ImageOps

from .api_logs import record_api_call
from .palette import BEAD_PALETTE
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

    last_network_error = None
    for attempt in range(3):
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
            last_network_error = error
            if attempt < 2:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            log_call(False, error=f"网络错误（已重试 3 次）：{type(error).__name__}")
            raise AIServiceError("无法连接图像 API，请检查接口地址和群晖网络") from error
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
            break
        await asyncio.sleep(0.8 * (attempt + 1))

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
        transparent_fraction = float(alpha_info["transparent_pixels"]) / max(int(alpha_info["total_pixels"]), 1)
        if transparent_fraction < MIN_TRANSPARENT_FRACTION:
            detail = (
                "图像编辑接口返回的 PNG 几乎没有透明背景（透明像素 "
                f"{alpha_info['transparent_percent']}%）。该兼容接口可能忽略了 background=transparent；"
                "请改用支持透明背景的图像编辑模型或选择本地识图。"
            )
            log_call(False, response=response, error=detail, alpha_info=alpha_info)
            raise AIServiceError(detail)
        log_call(True, response=response, output_bytes=len(result), alpha_info=alpha_info)
        return result
    except AIServiceError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        log_call(False, response=response, error="API 没有返回兼容的 b64_json 图片")
        raise AIServiceError("API 没有返回兼容的 b64_json 图片") from error


def _apply_source_alpha_to_reference(original_bytes: bytes, generated_bytes: bytes) -> bytes:
    """Keep the user's accepted cutout mask while taking only the generated RGB."""
    source = ImageOps.exif_transpose(Image.open(BytesIO(original_bytes))).convert("RGBA")
    generated = Image.open(BytesIO(generated_bytes)).convert("RGBA")
    if generated.size != source.size:
        generated = generated.resize(source.size, Image.Resampling.LANCZOS)
    generated.putalpha(source.getchannel("A"))
    output = BytesIO()
    generated.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def create_pattern_reference(
    image: bytes,
    filename: str,
    content_type: str,
) -> bytes:
    """Use the configured image model to simplify a source before deterministic bead matching."""
    try:
        settings = load_settings()
    except SettingsError as error:
        raise AIServiceError(str(error)) from error
    if not settings.enabled:
        raise AIServiceError("服务器尚未配置可用的 OpenAI 兼容 API")

    prompt = (
        "Create a clean reference image for a bead-pattern conversion from this exact input. "
        "Preserve every subject, silhouette, composition, pose, proportion, and important detail. "
        "Do not add, remove, move, redraw, beautify, or invent objects. "
        "Simplify only photographic texture, tiny noise, gradients, and antialiasing into clear solid colour regions with crisp boundaries. "
        "Keep the original palette relationship as closely as possible. "
        "This is not the final bead pattern: do not draw a grid, beads, symbols, labels, text, borders, or watermark. "
        "Keep any existing transparent background transparent. Return a PNG."
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
    call_id, dispatched, response = str(uuid4()), False, None
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    started_clock = time.monotonic()

    def log_call(success: bool, *, error: str | None = None, output_bytes: int | None = None) -> None:
        headers = getattr(response, "headers", {}) if response is not None else {}
        record_api_call({
            "id": call_id,
            "started_at": started_at,
            "operation": "image_pattern_reference",
            "provider": "OpenAI compatible",
            "endpoint": settings.edit_url,
            "model": settings.model,
            "dispatched": dispatched,
            "success": success,
            "duration_ms": round((time.monotonic() - started_clock) * 1000),
            "input": {"bytes": len(image), "content_type": content_type or "image/png"},
            "request": {
                "purpose": "bead_pattern_reference",
                "background": data["background"],
                "output_format": data["output_format"],
                "quality": data["quality"],
                "size": data["size"],
                "input_fidelity": data["input_fidelity"],
                "prompt_length": len(prompt),
            },
            "response": {
                "status_code": getattr(response, "status_code", None),
                "content_type": headers.get("content-type"),
                "request_id": headers.get("x-request-id") or headers.get("openai-request-id") or headers.get("request-id"),
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
        log_call(False, error=detail)
        raise AIServiceError(detail)

    try:
        encoded = response.json()["data"][0]["b64_json"]
        generated = base64.b64decode(encoded, validate=True)
        result = _apply_source_alpha_to_reference(image, generated)
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        log_call(False, error="图像 API 没有返回兼容的 b64_json 图片")
        raise AIServiceError("图像 API 没有返回兼容的 b64_json 图片") from error
    log_call(True, output_bytes=len(result))
    return result


async def generate_direct_bead_pattern(
    image: bytes,
    filename: str,
    content_type: str,
    width: int,
    height: int,
) -> bytes:
    """Ask Image2 for a final bead chart after the user has confirmed the cutout."""
    try:
        settings = load_settings()
    except SettingsError as error:
        raise AIServiceError(str(error)) from error
    if not settings.enabled:
        raise AIServiceError("服务器尚未配置可用的 OpenAI 兼容 API")

    palette_reference = "; ".join(f"{item['code']}={item['hex']}" for item in BEAD_PALETTE)
    prompt = (
        "Create a print-ready MARD 2.6 mm fused-bead pattern from the supplied isolated subject. "
        f"The chart must contain exactly {width} columns and {height} rows of equal square cells. "
        "Preserve the subject silhouette, pose, proportions and all recognizable details, but express it as clear discrete bead cells. "
        "Every occupied cell must use only one of the following MARD code and HEX pairs, filled with that exact HEX and printed with its exact code: "
        f"{palette_reference}. "
        "Leave only cells outside the subject blank white; do not use H2 for exterior background. "
        "Every enclosed or internal white region of the subject, including eyes, face fills, highlights, and holes, is a real bead area: "
        "fill it with the closest MARD white code and print that code in every occupied white cell. "
        "Draw only the rectangular bead grid and code labels. Do not add a title, legend, decorative border, watermark, prose, extra objects, or a second image. "
        "Return a high-resolution PNG of the complete chart."
    )
    data = {
        "model": settings.model,
        "prompt": prompt,
        "background": "opaque",
        "output_format": "png",
        "quality": settings.quality,
        "size": "auto",
        "input_fidelity": "high",
    }
    files = {"image": (filename or "subject.png", image, content_type or "image/png")}
    call_id, dispatched, response = str(uuid4()), False, None
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    started_clock = time.monotonic()

    def log_call(success: bool, *, error: str | None = None, output_bytes: int | None = None) -> None:
        headers = getattr(response, "headers", {}) if response is not None else {}
        record_api_call({
            "id": call_id,
            "started_at": started_at,
            "operation": "image_direct_pattern_generation",
            "provider": "OpenAI compatible",
            "endpoint": settings.edit_url,
            "model": settings.model,
            "dispatched": dispatched,
            "success": success,
            "duration_ms": round((time.monotonic() - started_clock) * 1000),
            "input": {"bytes": len(image), "content_type": content_type or "image/png", "board": f"{width}x{height}"},
            "request": {"purpose": "direct_bead_chart", "quality": data["quality"], "size": data["size"], "prompt_length": len(prompt)},
            "response": {
                "status_code": getattr(response, "status_code", None),
                "content_type": headers.get("content-type"),
                "request_id": headers.get("x-request-id") or headers.get("openai-request-id") or headers.get("request-id"),
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
        log_call(False, error=detail)
        raise AIServiceError(detail)

    try:
        encoded = response.json()["data"][0]["b64_json"]
        result = base64.b64decode(encoded, validate=True)
        Image.open(BytesIO(result)).verify()
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        log_call(False, error="图像 API 没有返回兼容的 b64_json PNG 图纸")
        raise AIServiceError("图像 API 没有返回兼容的 b64_json PNG 图纸") from error
    log_call(True, output_bytes=len(result))
    return result


def _material_rows(text: str) -> list[dict[str, int | str]]:
    try:
        parsed = json.loads(text.strip().strip(chr(96)).removeprefix("json").strip())
    except json.JSONDecodeError:
        return []
    raw = parsed.get("materials", []) if isinstance(parsed, dict) else []
    known_codes = {item["code"] for item in BEAD_PALETTE}
    merged: dict[str, int] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip().upper()
        try:
            count = int(item.get("count", 0))
        except (TypeError, ValueError):
            continue
        if code in known_codes and count > 0:
            merged[code] = merged.get(code, 0) + count
    return [{"code": code, "count": count} for code, count in sorted(merged.items())]


async def extract_direct_pattern_materials(chart: bytes, width: int, height: int) -> list[dict[str, int | str]]:
    """Ask the configured vision model to read Image2's rendered code grid."""
    try:
        settings = load_settings()
    except SettingsError as error:
        raise AIServiceError(str(error)) from error
    if not settings.vision_enabled:
        raise AIServiceError("服务器尚未配置可用的识图模型")
    prompt = (
        f"Read this {width} by {height} MARD bead chart. Count every visible occupied cell by its printed MARD code, "
        "including enclosed white regions inside the subject. Ignore unlabelled exterior blank cells. "
        'Return JSON only: {"materials":[{"code":"H2","count":12}]}. '
        "Only include codes that are visibly present; do not invent or estimate unreadable cells."
    )
    payload = {
        "model": settings.vision_model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(chart).decode("ascii"), "detail": "high"}},
        ]}],
        "temperature": 0,
    }
    call_id, dispatched, response = str(uuid4()), False, None
    started_at, clock = datetime.now(timezone.utc).isoformat(timespec="milliseconds"), time.monotonic()

    def log_call(success: bool, error: str | None = None) -> None:
        headers = getattr(response, "headers", {}) if response is not None else {}
        record_api_call({
            "id": call_id, "started_at": started_at, "operation": "vision_pattern_materials",
            "provider": "OpenAI compatible", "endpoint": settings.chat_url, "model": settings.vision_model,
            "dispatched": dispatched, "success": success, "duration_ms": round((time.monotonic() - clock) * 1000),
            "input": {"bytes": len(chart), "board": f"{width}x{height}"},
            "request": {"response_format": "json-instruction", "image_detail": "high"},
            "response": {"status_code": getattr(response, "status_code", None), "content_type": headers.get("content-type"), "request_id": headers.get("x-request-id") or headers.get("request-id")},
            "error": error,
        })

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0), follow_redirects=False) as client:
            dispatched = True
            response = await client.post(settings.chat_url, headers={"Authorization": f"Bearer {settings.api_key}"}, json=payload)
    except httpx.RequestError as error:
        log_call(False, f"网络错误：{type(error).__name__}")
        raise AIServiceError("无法连接材料清单识别 API") from error
    if response.status_code >= 400:
        detail = f"材料清单识别请求失败（{response.status_code}）"
        log_call(False, detail)
        raise AIServiceError(detail)
    materials = _material_rows(_chat_text(response.json()))
    if not materials:
        log_call(False, "识图模型没有返回可用的材料清单")
        raise AIServiceError("识图模型没有返回可用的材料清单")
    log_call(True)
    return materials
