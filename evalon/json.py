"""JSON helpers."""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Default payload limits (override via args or env)
DEFAULT_MAX_STR = 16_384
DEFAULT_MAX_LIST = 200
DEFAULT_MAX_DEPTH = 12

# Exact keys only — avoid matching prompt_tokens / max_tokens / etc.
_REDACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "authorization",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "cookie",
        "set_cookie",
        "private_key",
        "client_secret",
        "client_secret_key",
        "auth_token",
        "bearer",
        "x_api_key",
    }
)

# Keys that contain "token"/"secret" but are metrics or LLM params — never redact
_REDACT_ALLOWLIST = frozenset(
    {
        "max_tokens",
        "max_output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "prompt_tokens_details",
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_enabled() -> bool:
    val = os.environ.get("EVALON_REDACT", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def _should_redact_key(key: str) -> bool:
    if not _redact_enabled():
        return False
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _REDACT_ALLOWLIST:
        return False
    if normalized in _REDACT_KEYS:
        return True
    # Suffix patterns for nested secrets without matching *tokens metrics
    for suffix in ("_api_key", "_secret", "_password", "_passwd", "_private_key"):
        if normalized.endswith(suffix):
            return True
    if normalized.endswith("_token") and not normalized.endswith("_tokens"):
        return True
    return False


def _truncate_str(value: str, max_str: int) -> str:
    if max_str <= 0 or len(value) <= max_str:
        return value
    return f"{value[:max_str]}…[truncated {len(value) - max_str} chars]"


def sanitize(
    value: Any,
    *,
    max_str: int = DEFAULT_MAX_STR,
    max_list: int = DEFAULT_MAX_LIST,
    max_depth: int = DEFAULT_MAX_DEPTH,
    _depth: int = 0,
) -> Any:
    """Make a value JSON-serializable with size limits and optional redaction."""
    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, str):
        return _truncate_str(value, max_str)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, Path):
        return _truncate_str(str(value), max_str)

    if _depth >= max_depth:
        return f"…[max depth {max_depth}]"

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        items = list(value.items())
        truncated = False
        if max_list > 0 and len(items) > max_list:
            items = items[:max_list]
            truncated = True
        for k, v in items:
            key = str(k)
            if _should_redact_key(key):
                out[key] = "***"
            else:
                out[key] = sanitize(
                    v,
                    max_str=max_str,
                    max_list=max_list,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                )
        if truncated:
            out["…"] = f"[{len(value) - max_list} more keys truncated]"
        return out

    if isinstance(value, tuple | list | set):
        seq = list(value)
        truncated = False
        if max_list > 0 and len(seq) > max_list:
            seq = seq[:max_list]
            truncated = True
        out_list = [
            sanitize(
                v,
                max_str=max_str,
                max_list=max_list,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for v in seq
        ]
        if truncated:
            out_list.append(f"…[{len(value) - max_list} more items truncated]")
        return out_list

    if is_dataclass(value) and not isinstance(value, type):
        return sanitize(
            asdict(value),
            max_str=max_str,
            max_list=max_list,
            max_depth=max_depth,
            _depth=_depth + 1,
        )

    if hasattr(value, "model_dump"):
        try:
            return sanitize(
                value.model_dump(),
                max_str=max_str,
                max_list=max_list,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        except Exception:
            pass

    if hasattr(value, "dict") and callable(value.dict):
        try:
            return sanitize(
                value.dict(),
                max_str=max_str,
                max_list=max_list,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        except Exception:
            pass

    return _truncate_str(repr(value), max_str)
