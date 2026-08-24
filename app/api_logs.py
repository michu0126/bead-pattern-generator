from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import tempfile
import threading


_LOG_LOCK = threading.Lock()
_DEFAULT_MAX_ENTRIES = 200


def _log_path() -> Path:
    return Path(os.getenv("API_CALL_LOG_FILE", "/data/api-calls.jsonl"))


def _max_entries() -> int:
    try:
        configured = int(os.getenv("API_CALL_LOG_MAX_ENTRIES", str(_DEFAULT_MAX_ENTRIES)))
    except ValueError:
        configured = _DEFAULT_MAX_ENTRIES
    return min(max(configured, 20), 1000)


def _read_entries_unlocked() -> list[dict]:
    try:
        lines = _log_path().read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []

    entries: list[dict] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def _rewrite_unlocked(entries: list[dict]) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix="api-calls-", suffix=".jsonl", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            for entry in entries:
                output.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                output.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def record_api_call(entry: Mapping) -> bool:
    """Persist a sanitized API call record without affecting an image request."""
    safe_entry = dict(entry)
    for forbidden in ("api_key", "authorization", "image", "b64_json"):
        safe_entry.pop(forbidden, None)
    try:
        with _LOG_LOCK:
            entries = _read_entries_unlocked()
            entries.append(safe_entry)
            _rewrite_unlocked(entries[-_max_entries():])
        return True
    except OSError:
        return False


def list_api_calls(limit: int = 100) -> list[dict]:
    limit = min(max(int(limit), 1), _max_entries())
    with _LOG_LOCK:
        entries = _read_entries_unlocked()
    return list(reversed(entries[-limit:]))


def clear_api_calls() -> None:
    with _LOG_LOCK:
        try:
            _log_path().unlink()
        except FileNotFoundError:
            pass
