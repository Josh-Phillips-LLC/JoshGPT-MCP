#!/usr/bin/env python3
"""Context-agnostic supervisor capability MCP server.

This service evaluates escalation payloads from role workers.
It intentionally does not embed any role-specific mission context.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate
from mcp.server.fastmcp import FastMCP

DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8789
DEFAULT_TRANSPORT = "streamable-http"
ALLOWED_TRANSPORTS = {"stdio", "sse", "streamable-http"}

DEFAULT_REQUIRE_SHARED_TOKEN = True
DEFAULT_ESCALATE_ROLE = "executive-sponsor"

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts" / "supervisor" / "v1"
REQUEST_SCHEMA_PATH = SCHEMA_DIR / "request.schema.json"
RESPONSE_SCHEMA_PATH = SCHEMA_DIR / "response.schema.json"


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
        value = int(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}; using default {default}.", file=sys.stderr)
        return default
    if value <= 0:
        print(f"Invalid {name}={raw!r}; using default {default}.", file=sys.stderr)
        return default
    return value


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
    return json.loads(path.read_text(encoding="utf-8"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refusal_response(
    request_payload: dict[str, Any] | None,
    reason: str,
    *,
    escalate_to_role_slug: str | None = None,
) -> dict[str, Any]:
    payload = request_payload or {}
    return {
        "request_id": str(payload.get("request_id") or "unknown"),
        "task_id": str(payload.get("task_id") or "unknown"),
        "decision_id": str(uuid.uuid4()),
        "status": "refused",
        "decision": "deny",
        "rationale": "Supervisor capability refused request due to invalid or ambiguous context.",
        "constraints_to_apply": [],
        "questions_for_worker": [],
        "escalate_to_role_slug": escalate_to_role_slug,
        "refusal_reason": reason,
        "created_at": _now_iso(),
        "metadata": {},
    }


def _semantic_role_context_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    role_context = payload.get("role_context") if isinstance(payload, dict) else None
    if not isinstance(role_context, dict):
        return ["role_context missing or invalid"]

    supervisor_role_slug = str(payload.get("supervisor_role_slug") or "").strip()
    context_role_slug = str(role_context.get("role_slug") or "").strip()
    if not supervisor_role_slug:
        errors.append("supervisor_role_slug missing")
    if not context_role_slug:
        errors.append("role_context.role_slug missing")

    if supervisor_role_slug and context_role_slug and supervisor_role_slug != context_role_slug:
        errors.append("role_context.role_slug must match supervisor_role_slug")

    role_ref = str(role_context.get("role_job_description_ref") or "").strip()
    role_hash = str(role_context.get("role_job_description_sha256") or "").strip()
    if not role_ref:
        errors.append("role_context.role_job_description_ref missing")
    if not role_hash:
        errors.append("role_context.role_job_description_sha256 missing")

    constraints = role_context.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        errors.append("role_context.constraints must include at least one constraint")

    return errors


def _decision_for_payload(payload: dict[str, Any], *, default_escalate_role: str) -> dict[str, Any]:
    role_context = payload["role_context"]
    task_context = payload["task_context"]
    requested_decision = str(payload.get("requested_decision") or "auto").strip().lower()
    escalation_reason = str(payload.get("escalation_reason") or "other").strip().lower()
    question = str(payload.get("question") or "").strip().lower()

    authority_boundaries = {
        str(item).strip().lower() for item in role_context.get("authority_boundaries", [])
    }
    constraints_to_apply = [str(item) for item in role_context.get("constraints", [])]

    response = {
        "request_id": payload["request_id"],
        "task_id": payload["task_id"],
        "decision_id": str(uuid.uuid4()),
        "status": "ok",
        "decision": "allow",
        "rationale": "Supervisor capability approved continuation within role constraints.",
        "constraints_to_apply": constraints_to_apply,
        "questions_for_worker": [],
        "escalate_to_role_slug": None,
        "refusal_reason": None,
        "created_at": _now_iso(),
        "metadata": {
            "requested_decision": requested_decision,
            "escalation_reason": escalation_reason,
            "role_context_ref": role_context.get("role_job_description_ref"),
            "task_objective": task_context.get("objective"),
        },
    }

    if requested_decision in {"allow", "deny", "clarify", "escalate"}:
        response["decision"] = requested_decision
        response["rationale"] = "Supervisor capability honored explicit requested_decision override."

    if escalation_reason in {"instruction_ambiguity", "missing_required_input"}:
        response["decision"] = "clarify"
        response["rationale"] = "Worker must clarify ambiguity before continuing."
        response["questions_for_worker"] = [
            "Provide the missing instruction details and explicit acceptance criteria before execution continues."
        ]

    if escalation_reason == "authority_conflict":
        response["decision"] = "escalate"
        response["rationale"] = "Authority conflict requires escalation outside worker/supervisor pair."
        response["escalate_to_role_slug"] = default_escalate_role

    protected_change_detected = "protected" in question or "governance.md" in question
    if protected_change_detected and "protected-change-approval" not in authority_boundaries:
        response["decision"] = "escalate"
        response["rationale"] = (
            "Detected protected-change intent without explicit supervisor boundary approval marker."
        )
        response["escalate_to_role_slug"] = default_escalate_role

    return response


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
JOSHGPT_SUPERVISOR_DEFAULT_ESCALATE_ROLE = (
    os.getenv("JOSHGPT_SUPERVISOR_DEFAULT_ESCALATE_ROLE", DEFAULT_ESCALATE_ROLE).strip()
    or DEFAULT_ESCALATE_ROLE
)

if JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN and not JOSHGPT_SUPERVISOR_SHARED_TOKEN:
    raise ConfigError(
        "JOSHGPT_SUPERVISOR_SHARED_TOKEN must be set when "
        "JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN=true"
    )

REQUEST_SCHEMA = _load_json(REQUEST_SCHEMA_PATH)
RESPONSE_SCHEMA = _load_json(RESPONSE_SCHEMA_PATH)

mcp = FastMCP(
    "joshgpt-supervisor-capability",
    host=JOSHGPT_SUPERVISOR_BIND_HOST,
    port=JOSHGPT_SUPERVISOR_BIND_PORT,
)


def _assert_shared_token(shared_token: str) -> None:
    if not JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN:
        return
    if not shared_token or shared_token != JOSHGPT_SUPERVISOR_SHARED_TOKEN:
        raise PermissionError("invalid shared_token")


@mcp.tool()
def ask_supervisor_capability(payload: dict[str, Any], shared_token: str = "") -> dict[str, Any]:
    """Return deterministic supervisor decision for role-injected context payload."""

    _assert_shared_token(shared_token)

    try:
        validate(instance=payload, schema=REQUEST_SCHEMA)
    except ValidationError as exc:
        return _refusal_response(payload, f"schema validation failed: {exc.message}")

    semantic_errors = _semantic_role_context_errors(payload)
    if semantic_errors:
        return _refusal_response(payload, "; ".join(semantic_errors))

    response = _decision_for_payload(
        payload,
        default_escalate_role=JOSHGPT_SUPERVISOR_DEFAULT_ESCALATE_ROLE,
    )

    try:
        validate(instance=response, schema=RESPONSE_SCHEMA)
    except ValidationError as exc:
        return _refusal_response(
            payload,
            f"internal response validation failed: {exc.message}",
            escalate_to_role_slug=JOSHGPT_SUPERVISOR_DEFAULT_ESCALATE_ROLE,
        )

    return response


if __name__ == "__main__":
    print(
        (
            "Starting joshgpt-supervisor-capability with "
            f"transport={JOSHGPT_SUPERVISOR_TRANSPORT} "
            f"host={JOSHGPT_SUPERVISOR_BIND_HOST} "
            f"port={JOSHGPT_SUPERVISOR_BIND_PORT} "
            f"require_shared_token={JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN}"
        ),
        flush=True,
    )
    mcp.run(transport=JOSHGPT_SUPERVISOR_TRANSPORT)
