from __future__ import annotations

from functools import lru_cache
from io import BytesIO
import os

from PIL import Image, ImageOps
from rembg import new_session, remove


DEFAULT_MODEL = "isnet-general-use"
MIN_TRANSPARENT_FRACTION = 0.005


class LocalCutoutError(RuntimeError):
    pass


def model_name() -> str:
    return os.getenv("LOCAL_CUTOUT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


@lru_cache(maxsize=1)
def _session():
    try:
        return new_session(model_name())
    except Exception as error:
        raise LocalCutoutError(
            "容器本地分割模型无法加载；请重新拉取完整镜像后重建容器。"
        ) from error


def remove_background_locally(image_bytes: bytes) -> bytes:
    """Use the bundled offline segmentation model to return a transparent PNG."""
    try:
        source = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGBA")
    except (OSError, Image.DecompressionBombError) as error:
        raise LocalCutoutError("无法读取这张图片") from error

    try:
        result = remove(source, session=_session()).convert("RGBA")
    except LocalCutoutError:
        raise
    except Exception as error:
        raise LocalCutoutError("本地分割模型处理失败，请稍后重试") from error

    alpha = result.getchannel("A")
    histogram = alpha.histogram()
    transparent_fraction = sum(histogram[:250]) / max(alpha.width * alpha.height, 1)
    if transparent_fraction < MIN_TRANSPARENT_FRACTION:
        raise LocalCutoutError("本地分割模型未识别到可透明化的背景，请改用浏览器文字识图或云端模式。")

    output = BytesIO()
    result.save(output, format="PNG", optimize=True)
    return output.getvalue()
