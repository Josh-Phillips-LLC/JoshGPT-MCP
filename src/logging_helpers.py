"""Shared structured logging helpers for JoshGPT MCP services."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


_REDACTED = "<redacted>"
_SENSITIVE_KEYS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
    "cookie",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_sensitive_key(key: str) -> bool:
    lowered = str(key or "").strip().lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEYS)


def sanitize_value(value: Any, key_name: str = "") -> Any:
    if _is_sensitive_key(key_name):
        return _REDACTED
    if value is None:
        return None
    if isinstance(value, list):
        return [sanitize_value(item, key_name) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[str(k)] = sanitize_value(v, str(k))
        return out
    if isinstance(value, str) and len(value) > 4000:
        return f"{value[:4000]}...[truncated]"
    return value


def _as_correlation(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    chat_session_id = str(
        raw.get("chat_session_id")
        or raw.get("chatSessionId")
        or raw.get("x-chat-session-id")
        or ""
    ).strip()
    turn_id = str(
        raw.get("turn_id")
        or raw.get("turnId")
        or raw.get("x-turn-id")
        or ""
    ).strip()
    request_id = str(
        raw.get("request_id")
        or raw.get("requestId")
        or raw.get("x-request-id")
        or ""
    ).strip()
    tool_call_id = str(
        raw.get("tool_call_id")
        or raw.get("toolCallId")
        or raw.get("x-tool-call-id")
        or ""
    ).strip()
    return {
        "chat_session_id": chat_session_id,
        "turn_id": turn_id,
        "request_id": request_id,
        "tool_call_id": tool_call_id,
    }


def resolve_correlation(
    payload: Any = None,
    *,
    correlation: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, str], str]:
    """Resolve correlation in priority order: explicit > payload > headers > unknown."""

    explicit = _as_correlation(correlation or {})
    if explicit.get("chat_session_id") or explicit.get("turn_id"):
        return _fill_unknowns(explicit), "explicit"

    payload_corr: dict[str, str] = {}
    if isinstance(payload, dict):
        payload_corr = _as_correlation(payload.get("correlation") or {})
        if payload_corr.get("chat_session_id") or payload_corr.get("turn_id"):
            return _fill_unknowns(payload_corr), "payload"

    header_corr: dict[str, str] = {}
    if isinstance(headers, dict):
        header_corr = _as_correlation(
            {
                "chat_session_id": headers.get("x-chat-session-id", ""),
                "turn_id": headers.get("x-turn-id", ""),
                "request_id": headers.get("x-request-id", ""),
                "tool_call_id": headers.get("x-tool-call-id", ""),
            }
        )
        if header_corr.get("chat_session_id") or header_corr.get("turn_id"):
            return _fill_unknowns(header_corr), "headers"

    return _fill_unknowns({}), "unknown"


def _fill_unknowns(correlation: dict[str, str]) -> dict[str, str]:
    out = {
        "chat_session_id": str(correlation.get("chat_session_id") or "").strip(),
        "turn_id": str(correlation.get("turn_id") or "").strip(),
        "request_id": str(correlation.get("request_id") or "").strip(),
        "tool_call_id": str(correlation.get("tool_call_id") or "").strip(),
    }
    if not out["chat_session_id"]:
        out["chat_session_id"] = "unknown"
    if not out["turn_id"]:
        out["turn_id"] = "unknown"
    return out


def emit_log_event(
    *,
    service: str,
    event: str,
    message: str,
    level: str = "info",
    correlation: dict[str, str] | None = None,
    correlation_source: str = "unknown",
    component: str = "",
    operation: str = "",
    status: str = "",
    duration_ms: int | float | None = None,
    error_code: str = "",
    model: str = "",
    endpoint: str = "",
    source_path: str = "",
    attrs: dict[str, Any] | None = None,
) -> None:
    corr = _fill_unknowns(correlation or {})
    payload: dict[str, Any] = {
        "timestamp": _now_iso(),
        "service": str(service or "unknown-service"),
        "level": str(level or "info"),
        "event": str(event or "log"),
        "message": str(message or event or "log event"),
        "chat_session_id": corr["chat_session_id"],
        "turn_id": corr["turn_id"],
        "request_id": corr["request_id"],
        "tool_call_id": corr["tool_call_id"],
        "component": str(component or ""),
        "operation": str(operation or ""),
        "status": str(status or ""),
        "duration_ms": duration_ms,
        "error_code": str(error_code or ""),
        "correlation_source": str(correlation_source or "unknown"),
        "model": str(model or ""),
        "endpoint": str(endpoint or ""),
        "source_path": str(source_path or ""),
        "source": "docker",
        "env": str(os.getenv("LOG_ENV", "local")),
        "attrs": sanitize_value(attrs or {}),
    }

    # Remove empty optional fields.
    cleaned = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip() and key not in {
            "chat_session_id",
            "turn_id",
        }:
            continue
        if isinstance(value, dict) and not value:
            continue
        cleaned[key] = value

    print(json.dumps(cleaned, ensure_ascii=True), flush=True)
