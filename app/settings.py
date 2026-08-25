from __future__ import annotations

from dataclasses import asdict, dataclass
import hmac
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit, urlunsplit

import httpx


DEFAULT_API_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_VISION_MODEL = "gpt-5.5"
DEFAULT_QUALITY = "medium"


class SettingsError(RuntimeError):
    pass


class SettingsAuthError(SettingsError):
    pass


@dataclass(frozen=True)
class AISettings:
    api_url: str
    api_key: str
    model: str
    vision_model: str
    quality: str

    @property
    def edit_url(self) -> str:
        return image_edit_url(self.api_url)

    @property
    def chat_url(self) -> str:
        return chat_completion_url(self.api_url)

    @property
    def enabled(self) -> bool:
        return bool(self.api_url and self.api_key and self.model)

    @property
    def vision_enabled(self) -> bool:
        return bool(self.api_url and self.api_key and self.vision_model)


def _settings_path() -> Path:
    return Path(os.getenv("API_SETTINGS_FILE", "/data/settings.json"))


def normalize_api_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise SettingsError("API URL 不能为空")
    if len(value) > 2048:
        raise SettingsError("API URL 过长")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SettingsError("API URL 必须是有效的 http:// 或 https:// 地址")
    if parsed.username or parsed.password:
        raise SettingsError("API URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise SettingsError("API URL 不能包含查询参数或片段")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _api_base_url(api_url: str) -> str:
    normalized = normalize_api_url(api_url)
    for suffix in ("/images/edits", "/images/generations", "/chat/completions", "/responses", "/models"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def image_edit_url(api_url: str) -> str:
    return f"{_api_base_url(api_url)}/images/edits"


def chat_completion_url(api_url: str) -> str:
    return f"{_api_base_url(api_url)}/chat/completions"


def models_url(api_url: str) -> str:
    return f"{_api_base_url(api_url)}/models"


def _environment_settings() -> AISettings:
    return AISettings(
        api_url=os.getenv("OPENAI_API_URL", DEFAULT_API_URL).strip() or DEFAULT_API_URL,
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        model=os.getenv("OPENAI_IMAGE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        vision_model=os.getenv("OPENAI_VISION_MODEL", DEFAULT_VISION_MODEL).strip() or DEFAULT_VISION_MODEL,
        quality=os.getenv("OPENAI_IMAGE_QUALITY", DEFAULT_QUALITY).strip() or DEFAULT_QUALITY,
    )


def _looks_like_vision_model(model: str) -> bool:
    return model.lower().startswith(("gpt-5", "gpt-4", "o1", "o3", "o4"))


def load_settings() -> AISettings:
    fallback = _environment_settings()
    path = _settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    except (OSError, json.JSONDecodeError) as error:
        raise SettingsError("API 设置文件无法读取，请检查 /data 数据卷") from error

    if not isinstance(raw, dict):
        raise SettingsError("API 设置文件格式无效")

    saved_model = str(raw.get("model", fallback.model)).strip() or fallback.model
    saved_vision = str(raw.get("vision_model", "")).strip()
    # v0.7.0 以前只有一个 model 字段：把误填的 gpt-5.5 自动迁移为识图模型。
    if not saved_vision and _looks_like_vision_model(saved_model):
        saved_vision = saved_model
        saved_model = fallback.model

    return AISettings(
        api_url=str(raw.get("api_url", fallback.api_url)).strip() or fallback.api_url,
        api_key=str(raw.get("api_key", fallback.api_key)).strip(),
        model=saved_model,
        vision_model=saved_vision or fallback.vision_model,
        quality=str(raw.get("quality", fallback.quality)).strip() or fallback.quality,
    )


def save_settings(settings: AISettings) -> None:
    path = _settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix="settings-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(asdict(settings), output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise
    except OSError as error:
        raise SettingsError("API 设置无法保存，请确认 /data 数据卷可写") from error


def settings_password_configured() -> bool:
    return bool(os.getenv("SETTINGS_PASSWORD", "").strip())


def require_settings_password(provided: str | None) -> None:
    expected = os.getenv("SETTINGS_PASSWORD", "").strip()
    if not expected:
        raise SettingsAuthError("容器尚未设置 SETTINGS_PASSWORD，设置页面已锁定")
    if not provided or not hmac.compare_digest(provided, expected):
        raise SettingsAuthError("设置管理密码不正确")


def public_settings(settings: AISettings) -> dict:
    return {
        "api_url": settings.api_url,
        "model": settings.model,
        "vision_model": settings.vision_model,
        "quality": settings.quality,
        "has_api_key": bool(settings.api_key),
        "enabled": settings.enabled,
        "vision_enabled": settings.vision_enabled,
    }


async def test_api_connection(settings: AISettings) -> dict:
    if not settings.api_key:
        raise SettingsError("请先输入 API Key")
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False) as client:
            response = await client.get(models_url(settings.api_url), headers=headers)
    except httpx.RequestError as error:
        raise SettingsError("无法连接该 API 地址，请检查 URL、网络或证书") from error

    if response.status_code >= 400:
        message = None
        try:
            body = response.json()
            message = body.get("error", {}).get("message") if isinstance(body, dict) else None
        except ValueError:
            pass
        detail = str(message)[:300] if message else f"HTTP {response.status_code}"
        raise SettingsError(f"API 验证失败：{detail}")
    try:
        body = response.json()
        raw_models = body.get("data", []) if isinstance(body, dict) else []
        models = sorted({str(item.get("id", "")).strip() for item in raw_models if isinstance(item, dict) and str(item.get("id", "")).strip()})
    except ValueError:
        models = []
    return {"ok": True, "message": f"连接成功，/models 返回 {len(models)} 个模型", "models": models}
