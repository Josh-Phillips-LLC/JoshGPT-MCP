#!/usr/bin/env python3
"""Codex supervisor capability MCP server.

This service exposes ask_codex_supervisor using governed request/response schemas.
Runtime backend for this phase is Codex CLI only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP

DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8789
DEFAULT_TRANSPORT = "streamable-http"
ALLOWED_TRANSPORTS = {"stdio", "sse", "streamable-http"}

DEFAULT_REQUIRE_SHARED_TOKEN = True
DEFAULT_CODEX_CLI_BIN = "codex"
DEFAULT_CODEX_CLI_TIMEOUT_SECONDS = 90.0
DEFAULT_CODEX_MODEL = "gpt-5"

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts"
REQUEST_SCHEMA_PATH = CONTRACT_DIR / "ask-codex-supervisor.request.schema.json"
RESPONSE_SCHEMA_PATH = CONTRACT_DIR / "ask-codex-supervisor.response.schema.json"


class ConfigError(RuntimeError):
    """Invalid runtime configuration."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    print(f"Invalid {name}={raw!r}; using default {default}.", file=sys.stderr)
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}; using default {default}.", file=sys.stderr)
        return default
    if parsed <= 0:
        print(f"Invalid {name}={raw!r}; using default {default}.", file=sys.stderr)
        return default
    return parsed


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}; using default {default}.", file=sys.stderr)
        return default
    if parsed <= 0:
        print(f"Invalid {name}={raw!r}; using default {default}.", file=sys.stderr)
        return default
    return parsed


def _resolve_transport(raw_transport: str) -> str:
    normalized = raw_transport.strip().lower()
    if normalized in ALLOWED_TRANSPORTS:
        return normalized
    print(
        f"Invalid JOSHGPT_SUPERVISOR_TRANSPORT={raw_transport!r}; "
        f"defaulting to {DEFAULT_TRANSPORT}.",
        file=sys.stderr,
    )
    return DEFAULT_TRANSPORT


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Schema file missing: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        if lines and lines[0].strip().lower() == "json":
            lines = lines[1:]
        cleaned = "\n".join(lines).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in Codex response text")

    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Codex response JSON was not an object")
    return parsed


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for tag in tags:
        if tag in seen:
            continue
        output.append(tag)
        seen.add(tag)
    return output


def _fail_safe_response(reason: str, label: str) -> dict[str, Any]:
    return {
        "decision": "pause_for_human",
        "rationale": (
            "Codex supervisor fail-safe response: "
            f"{reason} ({label}). Human review is required before continuing."
        ),
        "next_actions": [
            "Capture current objective, blocked reason, and evidence summary.",
            "Verify Codex CLI availability and login status in supervisor runtime.",
            "Retry escalation after resolving the runtime/configuration issue.",
        ],
        "confidence": 0.1,
        "safety_checks": [
            "Authorized scope present",
            "Escalation reason explicit",
            "No out-of-scope action requested",
        ],
        "audit_tags": _dedupe_tags([
            "fail-safe",
            f"error:{label}",
            "manual-review-required",
        ]),
    }


def _error_label(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "codex-timeout"
    if isinstance(exc, FileNotFoundError):
        return "codex-cli-not-found"
    if isinstance(exc, json.JSONDecodeError):
        return "json-decode-error"
    return exc.__class__.__name__.lower()


def _assert_shared_token(shared_token: str) -> None:
    if not JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN:
        return
    if not shared_token or shared_token != JOSHGPT_SUPERVISOR_SHARED_TOKEN:
        raise PermissionError("invalid shared_token")


def _resolve_codex_bin() -> str:
    resolved = shutil.which(JOSHGPT_SUPERVISOR_CODEX_CLI_BIN)
    if resolved:
        return resolved

    candidate = Path(JOSHGPT_SUPERVISOR_CODEX_CLI_BIN)
    if candidate.is_absolute() and candidate.exists() and candidate.is_file():
        return str(candidate)

    raise FileNotFoundError(
        f"codex cli binary not found: {JOSHGPT_SUPERVISOR_CODEX_CLI_BIN}"
    )


def _ensure_codex_login_status(codex_bin: str) -> None:
    completed = subprocess.run(
        [codex_bin, "login", "status"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"codex login status failed: {detail[:300]}")


def _build_codex_exec_prompt(payload: dict[str, Any]) -> str:
    return (
        "You are a supervisor agent. Return only one JSON object that strictly matches "
        "the provided response schema. No markdown and no extra prose. "
        "If uncertain, choose decision=pause_for_human.\n\n"
        "Escalation payload JSON:\n"
        f"{json.dumps(payload, ensure_ascii=True, separators=(',', ':'))}"
    )


def _call_codex_cli(payload: dict[str, Any]) -> dict[str, Any]:
    codex_bin = _resolve_codex_bin()
    _ensure_codex_login_status(codex_bin)

    tmp = tempfile.NamedTemporaryFile(
        prefix="codex-supervisor-",
        suffix=".json",
        delete=False,
    )
    output_path = Path(tmp.name)
    tmp.close()

    cmd = [
        codex_bin,
        "-a",
        "never",
        "-m",
        JOSHGPT_SUPERVISOR_CODEX_MODEL,
        "exec",
        "-",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(RESPONSE_SCHEMA_PATH),
        "--output-last-message",
        str(output_path),
        "--color",
        "never",
    ]

    try:
        completed = subprocess.run(
            cmd,
            input=_build_codex_exec_prompt(payload),
            capture_output=True,
            text=True,
            timeout=JOSHGPT_SUPERVISOR_CODEX_CLI_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"codex exec failed: {detail[:500]}")

        raw_output = output_path.read_text(encoding="utf-8").strip()
        if not raw_output:
            raise ValueError("codex exec produced empty output")

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            parsed = _extract_json_object(raw_output)

        if not isinstance(parsed, dict):
            raise ValueError("codex exec output was not a JSON object")

        existing_tags = parsed.get("audit_tags")
        normalized_tags = (
            [tag for tag in existing_tags if isinstance(tag, str) and tag.strip()]
            if isinstance(existing_tags, list)
            else []
        )
        parsed["audit_tags"] = _dedupe_tags(
            normalized_tags + ["provider-codex-cli", "proxy-enabled"]
        )
        return parsed
    finally:
        output_path.unlink(missing_ok=True)


JOSHGPT_SUPERVISOR_BIND_HOST = (
    os.getenv("JOSHGPT_SUPERVISOR_BIND_HOST", DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
)
JOSHGPT_SUPERVISOR_BIND_PORT = _env_int("JOSHGPT_SUPERVISOR_BIND_PORT", DEFAULT_BIND_PORT)
JOSHGPT_SUPERVISOR_TRANSPORT = _resolve_transport(
    os.getenv("JOSHGPT_SUPERVISOR_TRANSPORT", DEFAULT_TRANSPORT)
)
JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN = _env_bool(
    "JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN", DEFAULT_REQUIRE_SHARED_TOKEN
)
JOSHGPT_SUPERVISOR_SHARED_TOKEN = os.getenv("JOSHGPT_SUPERVISOR_SHARED_TOKEN", "")
JOSHGPT_SUPERVISOR_CODEX_CLI_BIN = (
    os.getenv("JOSHGPT_SUPERVISOR_CODEX_CLI_BIN", DEFAULT_CODEX_CLI_BIN).strip()
    or DEFAULT_CODEX_CLI_BIN
)
JOSHGPT_SUPERVISOR_CODEX_CLI_TIMEOUT_SECONDS = _env_float(
    "JOSHGPT_SUPERVISOR_CODEX_CLI_TIMEOUT_SECONDS",
    DEFAULT_CODEX_CLI_TIMEOUT_SECONDS,
)
JOSHGPT_SUPERVISOR_CODEX_MODEL = (
    os.getenv("JOSHGPT_SUPERVISOR_CODEX_MODEL", DEFAULT_CODEX_MODEL).strip()
    or DEFAULT_CODEX_MODEL
)

if JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN and not JOSHGPT_SUPERVISOR_SHARED_TOKEN:
    raise ConfigError(
        "JOSHGPT_SUPERVISOR_SHARED_TOKEN must be set when "
        "JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN=true"
    )

REQUEST_VALIDATOR = Draft202012Validator(_load_json(REQUEST_SCHEMA_PATH))
RESPONSE_VALIDATOR = Draft202012Validator(_load_json(RESPONSE_SCHEMA_PATH))

mcp = FastMCP(
    "joshgpt-supervisor-capability",
    host=JOSHGPT_SUPERVISOR_BIND_HOST,
    port=JOSHGPT_SUPERVISOR_BIND_PORT,
)


@mcp.tool()
def ask_codex_supervisor(payload: dict[str, Any], shared_token: str = "") -> dict[str, Any]:
    """Escalate worker context to Codex supervisor (CLI backend)."""

    _assert_shared_token(shared_token)

    try:
        REQUEST_VALIDATOR.validate(payload)
    except Exception as exc:
        response = _fail_safe_response(
            "request validation failed",
            _error_label(exc),
        )
        RESPONSE_VALIDATOR.validate(response)
        return response

    try:
        response = _call_codex_cli(payload)
        RESPONSE_VALIDATOR.validate(response)
        return response
    except Exception as exc:
        fail_safe = _fail_safe_response(
            "codex supervisor runtime failed",
            _error_label(exc),
        )
        RESPONSE_VALIDATOR.validate(fail_safe)
        return fail_safe


if __name__ == "__main__":
    print(
        (
            "Starting joshgpt-supervisor-capability with "
            f"transport={JOSHGPT_SUPERVISOR_TRANSPORT} "
            f"host={JOSHGPT_SUPERVISOR_BIND_HOST} "
            f"port={JOSHGPT_SUPERVISOR_BIND_PORT} "
            f"require_shared_token={JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN} "
            f"codex_cli_bin={JOSHGPT_SUPERVISOR_CODEX_CLI_BIN} "
            f"codex_model={JOSHGPT_SUPERVISOR_CODEX_MODEL}"
        ),
        flush=True,
    )
    mcp.run(transport=JOSHGPT_SUPERVISOR_TRANSPORT)
