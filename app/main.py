from __future__ import annotations

import base64
from io import BytesIO

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from .ai import AIServiceError, ai_configured, image_model, remove_background, suggest_cutout_subjects, vision_model
from .api_logs import clear_api_calls, list_api_calls
from .palette import BEAD_PALETTE
from .pattern import generate_pattern
from .settings import (
    AISettings,
    SettingsAuthError,
    SettingsError,
    load_settings,
    normalize_api_url,
    public_settings,
    require_settings_password,
    save_settings,
    settings_password_configured,
    test_api_connection,
)

APP_VERSION = "0.9.0"
VERSION_CHANGES = [
    "板型适配常用的 52、72、78、104 钉正方形单板",
    "板型选择改为规格卡片，可直观看到尺寸和当前选项",
    "默认板型改为 52 × 52 钉单板",
]

BOARD_SPECS = {
    "52x52": {"id": "52x52", "label": "52 钉单板", "width": 52, "height": 52},
    "72x72": {"id": "72x72", "label": "72 钉单板", "width": 72, "height": 72},
    "78x78": {"id": "78x78", "label": "78 钉单板", "width": 78, "height": 78},
    "104x104": {"id": "104x104", "label": "104 钉单板", "width": 104, "height": 104},
}


class APISettingsPayload(BaseModel):
    api_url: str = Field(min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    model: str = Field(min_length=1, max_length=200)
    vision_model: str = Field(default="gpt-5.5", min_length=1, max_length=200)
    quality: str = Field(default="medium", min_length=1, max_length=40)
    clear_api_key: bool = False

app = FastAPI(title="拼豆图纸生成器", version=APP_VERSION)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if (
        request.url.path in {"/", "/api/version", "/api/config"}
        or request.url.path.startswith("/static/")
        or request.url.path.startswith("/api/settings")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/version")
def version() -> dict:
    return {"version": APP_VERSION, "changes": VERSION_CHANGES}


@app.post("/api/cache/clear")
def clear_cache() -> JSONResponse:
    response = JSONResponse({"status": "ok", "version": APP_VERSION})
    response.headers["Clear-Site-Data"] = '"cache"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/boards")
def boards() -> list[dict]:
    return list(BOARD_SPECS.values())


@app.get("/api/config")
def config() -> dict:
    try:
        enabled = ai_configured()
        model = image_model() if enabled else None
        vision = vision_model() if enabled else None
    except SettingsError:
        enabled = False
        model = None
        vision = None
    return {
        "ai_enabled": enabled,
        "ai_model": model,
        "vision_model": vision,
        "settings_enabled": settings_password_configured(),
    }


def _authorize_settings(password: str | None) -> None:
    try:
        require_settings_password(password)
    except SettingsAuthError as error:
        status = 503 if not settings_password_configured() else 401
        raise HTTPException(status, str(error)) from None


def _settings_from_payload(payload: APISettingsPayload) -> AISettings:
    current = load_settings()
    if payload.clear_api_key:
        api_key = ""
    elif payload.api_key and payload.api_key.strip():
        api_key = payload.api_key.strip()
    else:
        api_key = current.api_key
    model = payload.model.strip()
    vision = payload.vision_model.strip()
    quality = payload.quality.strip()
    if not model:
        raise SettingsError("图像模型不能为空")
    if not vision:
        raise SettingsError("识图模型不能为空")
    if not quality:
        raise SettingsError("图像质量不能为空")
    return AISettings(
        api_url=normalize_api_url(payload.api_url),
        api_key=api_key,
        model=model,
        vision_model=vision,
        quality=quality,
    )


@app.get("/api/settings")
def get_api_settings(x_settings_password: str | None = Header(default=None)) -> dict:
    _authorize_settings(x_settings_password)
    try:
        return public_settings(load_settings())
    except SettingsError as error:
        raise HTTPException(500, str(error)) from None


@app.put("/api/settings")
def update_api_settings(
    payload: APISettingsPayload,
    x_settings_password: str | None = Header(default=None),
) -> dict:
    _authorize_settings(x_settings_password)
    try:
        settings = _settings_from_payload(payload)
        save_settings(settings)
        saved = load_settings()
        if saved != settings:
            raise SettingsError("API 设置写入后校验失败，请检查 /data 数据卷")
        return {**public_settings(saved), "message": "API 设置已保存并校验"}
    except SettingsError as error:
        raise HTTPException(400, str(error)) from None


@app.delete("/api/settings/key")
def delete_api_key(x_settings_password: str | None = Header(default=None)) -> dict:
    _authorize_settings(x_settings_password)
    try:
        current = load_settings()
        settings = AISettings(
            api_url=current.api_url,
            api_key="",
            model=current.model,
            vision_model=current.vision_model,
            quality=current.quality,
        )
        save_settings(settings)
        return {**public_settings(settings), "message": "已删除保存的 API Key"}
    except SettingsError as error:
        raise HTTPException(400, str(error)) from None


@app.get("/api/settings/logs")
def get_api_call_logs(
    limit: int = 100,
    x_settings_password: str | None = Header(default=None),
) -> dict:
    _authorize_settings(x_settings_password)
    return {"logs": list_api_calls(limit)}


@app.delete("/api/settings/logs")
def delete_api_call_logs(x_settings_password: str | None = Header(default=None)) -> dict:
    _authorize_settings(x_settings_password)
    clear_api_calls()
    return {"message": "API 调用日志已清空"}


@app.post("/api/settings/test")
async def test_api_settings(
    payload: APISettingsPayload,
    x_settings_password: str | None = Header(default=None),
) -> dict:
    _authorize_settings(x_settings_password)
    try:
        settings = _settings_from_payload(payload)
        return await test_api_connection(settings)
    except SettingsError as error:
        raise HTTPException(400, str(error)) from None


@app.post("/api/ai/subjects")
async def ai_subjects(
    image: UploadFile = File(...),
    prompt: str = Form("", max_length=500),
) -> dict:
    raw = await image.read()
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(413, "图片不能超过 12 MB")
    try:
        source = Image.open(BytesIO(raw))
        source.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, "无法识别这张图片") from None
    try:
        subjects = await suggest_cutout_subjects(raw, image.content_type or "image/png", prompt)
    except AIServiceError as error:
        status = 503 if "尚未配置" in str(error) else 502
        raise HTTPException(status, str(error)) from None
    return {"model": vision_model(), "subjects": subjects}


@app.post("/api/ai/cutout")
async def ai_cutout(
    image: UploadFile = File(...),
    prompt: str = Form("", max_length=500),
) -> dict:
    raw = await image.read()
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(413, "图片不能超过 12 MB")
    try:
        source = Image.open(BytesIO(raw))
        source.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, "无法识别这张图片") from None
    try:
        result = await remove_background(
            raw,
            image.filename or "image.png",
            image.content_type or "image/png",
            prompt,
        )
    except AIServiceError as error:
        status = 503 if "尚未配置" in str(error) else 502
        raise HTTPException(status, str(error)) from None
    return {
        "model": image_model(),
        "image": "data:image/png;base64," + base64.b64encode(result).decode("ascii"),
    }


@app.post("/api/generate")
async def generate(
    image: UploadFile = File(...),
    board: str = Form("52x52"),
) -> dict:
    spec = BOARD_SPECS.get(board)
    if spec is None:
        raise HTTPException(400, "请选择有效的板子规格")
    width, height = spec["width"], spec["height"]
    raw = await image.read()
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(413, "图片不能超过 12 MB")
    try:
        source = Image.open(BytesIO(raw))
        pattern, summary, grid = await run_in_threadpool(generate_pattern, source, width, height)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, "无法识别这张图片") from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    total = sum(item["count"] for item in summary)
    return {
        "board": spec,
        "width": width,
        "height": height,
        "total": total,
        "empty": width * height - total,
        "palette": summary,
        "colours": [
            {
                "code": item["code"],
                "name": item["name"],
                "hex": item["hex"],
                "rgb": list(item["rgb"]),
            }
            for item in BEAD_PALETTE
        ],
        "grid": grid,
        "image": "data:image/png;base64," + base64.b64encode(pattern).decode("ascii"),
    }


app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse("app/static/index.html")
