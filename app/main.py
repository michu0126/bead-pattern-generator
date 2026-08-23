from __future__ import annotations

import base64
from io import BytesIO

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from .pattern import generate_pattern

APP_VERSION = "0.3.0"
VERSION_CHANGES = [
    "新增一键清除页面缓存并刷新",
    "页面和静态资源默认不缓存，容器更新后立即显示新版",
    "新增固定版本号与版本更新内容提示",
    "保留 v0.2.0 的板型选项、MARD 221 色匹配和文字抠图",
]

BOARD_SPECS = {
    "50x50": {"id": "50x50", "label": "50 × 50（2.6 mm 标准单板）", "width": 50, "height": 50},
    "52x52": {"id": "52x52", "label": "52 × 52（2.6 mm 标准单板）", "width": 52, "height": 52},
    "100x50": {"id": "100x50", "label": "100 × 50（两块 50 板横拼）", "width": 100, "height": 50},
    "104x52": {"id": "104x52", "label": "104 × 52（两块 52 板横拼）", "width": 104, "height": 52},
    "100x100": {"id": "100x100", "label": "100 × 100（四块 50 板）", "width": 100, "height": 100},
    "104x104": {"id": "104x104", "label": "104 × 104（四块 52 板）", "width": 104, "height": 104},
}

app = FastAPI(title="拼豆图纸生成器", version=APP_VERSION)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path in {"/", "/api/version"} or request.url.path.startswith("/static/"):
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


@app.post("/api/generate")
async def generate(
    image: UploadFile = File(...),
    board: str = Form("50x50"),
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
        pattern, summary = await run_in_threadpool(generate_pattern, source, width, height)
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
        "image": "data:image/png;base64," + base64.b64encode(pattern).decode("ascii"),
    }


app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse("app/static/index.html")
