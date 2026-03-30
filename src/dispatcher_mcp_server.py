#!/usr/bin/env python3
"""Dispatcher MCP server for role-routed task execution.

MVP behavior:
- Stores tasks/events in SQLite.
- Routes worker tasks with explicit supervisor role assignment.
- Tracks supervisor Q/A events linked to task IDs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from hashlib import sha256
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from mcp.server.fastmcp import Context, FastMCP
import yaml
from logging_helpers import emit_log_event, resolve_correlation

DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8788
DEFAULT_TRANSPORT = "streamable-http"
ALLOWED_TRANSPORTS = {"stdio", "sse", "streamable-http"}

DEFAULT_DB_PATH = "/tmp/joshgpt_dispatcher.db"
DEFAULT_REQUIRE_SHARED_TOKEN = True
DEFAULT_ROLE_REGISTRY_PATH = "/registry/role-registry.yml"
DEFAULT_ROLE_REPOS_BASE_PATH = "/role-repos"
DEFAULT_SUPERVISOR_CONTEXT_MAX_CHARS = 12000

ALLOWED_TASK_STATUSES = {
    "queued",
    "claimed",
    "running",
    "awaiting_supervisor",
    "completed",
    "failed",
    "canceled",
}


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
        f"Invalid JOSHGPT_DISPATCHER_TRANSPORT={raw_transport!r}; "
        f"defaulting to {DEFAULT_TRANSPORT}.",
        file=sys.stderr,
    )
    return DEFAULT_TRANSPORT


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _require_non_empty(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    return cleaned


def _ensure_status(status: str) -> str:
    cleaned = status.strip().lower()
    if cleaned not in ALLOWED_TASK_STATUSES:
        raise ValueError(
            f"Unsupported status {status!r}. Allowed statuses: {', '.join(sorted(ALLOWED_TASK_STATUSES))}"
        )
    return cleaned


def _ensure_list_of_strings(items: list[str] | None, field: str) -> list[str]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError(f"{field} must be an array of strings")
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _truncate_excerpt(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars]


def _read_required_text(path: Path, label: str) -> str:
    if not path.exists():
        raise FileNotFoundError(f"{label} missing: {path}")
    if not path.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    return path.read_text(encoding="utf-8")


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.time() - started_at) * 1000))


def _error_code(exc: Exception) -> str:
    return exc.__class__.__name__.strip().lower() or "error"


def _extract_headers(ctx: Context | None) -> dict[str, str]:
    if ctx is None:
        return {}
    try:
        request = ctx.request_context.request
    except Exception:
        return {}
    if request is None:
        return {}
    raw_headers = getattr(request, "headers", None)
    if not raw_headers or not hasattr(raw_headers, "items"):
        return {}
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        headers[str(key).lower()] = str(value)
    return headers


def _resolve_call_correlation(
    *,
    payload: Any = None,
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> tuple[dict[str, str], str]:
    return resolve_correlation(
        payload,
        correlation=correlation or {},
        headers=_extract_headers(ctx),
    )


def _emit_dispatcher_event(
    *,
    event: str,
    message: str,
    correlation: dict[str, str],
    correlation_source: str,
    level: str = "info",
    operation: str = "",
    status: str = "",
    duration_ms: int | None = None,
    error_code: str = "",
    attrs: dict[str, Any] | None = None,
) -> None:
    emit_log_event(
        service="dispatcher",
        event=event,
        message=message,
        level=level,
        correlation=correlation,
        correlation_source=correlation_source,
        component="dispatcher",
        operation=operation,
        status=status,
        duration_ms=duration_ms,
        error_code=error_code,
        attrs=attrs or {},
    )


JOSHGPT_DISPATCHER_BIND_HOST = (
    os.getenv("JOSHGPT_DISPATCHER_BIND_HOST", DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
)
JOSHGPT_DISPATCHER_BIND_PORT = _env_int("JOSHGPT_DISPATCHER_BIND_PORT", DEFAULT_BIND_PORT)
JOSHGPT_DISPATCHER_TRANSPORT = _resolve_transport(
    os.getenv("JOSHGPT_DISPATCHER_TRANSPORT", DEFAULT_TRANSPORT)
)
JOSHGPT_DISPATCHER_DB_PATH = Path(
    os.getenv("JOSHGPT_DISPATCHER_DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH
).expanduser()
JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN = _env_bool(
    "JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN", DEFAULT_REQUIRE_SHARED_TOKEN
)
JOSHGPT_DISPATCHER_SHARED_TOKEN = os.getenv("JOSHGPT_DISPATCHER_SHARED_TOKEN", "")
JOSHGPT_ROLE_REGISTRY_PATH = Path(
    os.getenv("JOSHGPT_ROLE_REGISTRY_PATH", DEFAULT_ROLE_REGISTRY_PATH).strip()
    or DEFAULT_ROLE_REGISTRY_PATH
).expanduser()
JOSHGPT_ROLE_REPOS_BASE_PATH = Path(
    os.getenv("JOSHGPT_ROLE_REPOS_BASE_PATH", DEFAULT_ROLE_REPOS_BASE_PATH).strip()
    or DEFAULT_ROLE_REPOS_BASE_PATH
).expanduser()
JOSHGPT_SUPERVISOR_CONTEXT_MAX_CHARS = _env_int(
    "JOSHGPT_SUPERVISOR_CONTEXT_MAX_CHARS",
    DEFAULT_SUPERVISOR_CONTEXT_MAX_CHARS,
)

if JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN and not JOSHGPT_DISPATCHER_SHARED_TOKEN:
    raise ConfigError(
        "JOSHGPT_DISPATCHER_SHARED_TOKEN must be set when "
        "JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN=true"
    )

JOSHGPT_DISPATCHER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(JOSHGPT_DISPATCHER_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                worker_role_slug TEXT NOT NULL,
                supervisor_role_slug TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                constraints_json TEXT NOT NULL,
                input_refs_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_role_slug TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_worker ON tasks(status, worker_role_slug);

            CREATE TABLE IF NOT EXISTS supervisor_messages (
                message_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                from_role_slug TEXT NOT NULL,
                to_role_slug TEXT NOT NULL,
                escalation_reason TEXT NOT NULL,
                question TEXT NOT NULL,
                role_context_ref TEXT NOT NULL,
                role_context_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                response_json TEXT,
                created_at TEXT NOT NULL,
                responded_at TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_supervisor_messages_task_id ON supervisor_messages(task_id);
            CREATE INDEX IF NOT EXISTS idx_supervisor_messages_status_to_role
                ON supervisor_messages(status, to_role_slug);
            """
        )


def _assert_shared_token(shared_token: str) -> None:
    if not JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN:
        return
    if not shared_token or shared_token != JOSHGPT_DISPATCHER_SHARED_TOKEN:
        raise PermissionError("invalid shared_token")


def _record_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    event_type: str,
    actor_role_slug: str | None,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO task_events (task_id, event_type, actor_role_slug, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, event_type, actor_role_slug, _json_dumps(payload), _now_iso()),
    )


def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "worker_role_slug": row["worker_role_slug"],
        "supervisor_role_slug": row["supervisor_role_slug"],
        "objective": row["objective"],
        "status": row["status"],
        "constraints": _json_loads(row["constraints_json"], []),
        "input_refs": _json_loads(row["input_refs_json"], []),
        "payload": _json_loads(row["payload_json"], {}),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _fetch_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise LookupError(f"task not found: {task_id}")
    return row


def _load_role_catalog() -> dict[str, Any]:
    raw_registry = _read_required_text(JOSHGPT_ROLE_REGISTRY_PATH, "role registry")
    parsed = yaml.safe_load(raw_registry)
    if not isinstance(parsed, dict):
        raise ValueError("role registry must be a YAML object")

    metadata = parsed.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("role registry metadata section missing or invalid")

    roles = parsed.get("roles")
    if not isinstance(roles, list):
        raise ValueError("role registry roles section missing or invalid")

    registry_source = str(metadata.get("canonical_source", "")).strip() or str(
        JOSHGPT_ROLE_REGISTRY_PATH
    )
    registry_version = _require_non_empty(str(metadata.get("version", "")), "metadata.version")

    normalized_roles: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for index, role in enumerate(roles):
        if not isinstance(role, dict):
            raise ValueError(f"role entry at index={index} must be an object")

        slug = _require_non_empty(str(role.get("slug", "")), f"roles[{index}].slug")
        if slug in seen_slugs:
            raise ValueError(f"duplicate role slug in registry: {slug}")
        seen_slugs.add(slug)

        display_name = _require_non_empty(
            str(role.get("display_name", "")),
            f"roles[{index}].display_name",
        )
        repo_name = _require_non_empty(str(role.get("repo_name", "")), f"roles[{index}].repo_name")
        try:
            menu_order = int(role.get("menu_order"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"roles[{index}].menu_order must be an integer") from exc

        normalized_roles.append(
            {
                "slug": slug,
                "display_name": display_name,
                "repo_name": repo_name,
                "menu_order": menu_order,
            }
        )

    normalized_roles.sort(key=lambda item: (item["menu_order"], item["display_name"].lower(), item["slug"]))

    return {
        "registry_source": registry_source,
        "registry_version": registry_version,
        "roles": normalized_roles,
    }


def _resolve_supervisor_role_context(role_slug: str) -> dict[str, Any]:
    role = _require_non_empty(role_slug, "role_slug")
    catalog = _load_role_catalog()

    role_info = next((entry for entry in catalog["roles"] if entry["slug"] == role), None)
    if role_info is None:
        raise LookupError(f"role not found in registry: {role}")

    role_repo_path = JOSHGPT_ROLE_REPOS_BASE_PATH / role_info["repo_name"]
    agents_path = role_repo_path / "AGENTS.md"
    runtime_policy_path = role_repo_path / ".github" / "copilot-instructions.md"

    agents_text = _read_required_text(agents_path, "role AGENTS.md")
    runtime_policy_text = _read_required_text(runtime_policy_path, "runtime policy adapter")

    agents_sha256 = _sha256_text(agents_text)
    runtime_policy_sha256 = _sha256_text(runtime_policy_text)

    agents_excerpt = _truncate_excerpt(agents_text, JOSHGPT_SUPERVISOR_CONTEXT_MAX_CHARS)
    runtime_policy_excerpt = _truncate_excerpt(
        runtime_policy_text,
        JOSHGPT_SUPERVISOR_CONTEXT_MAX_CHARS,
    )

    agents_ref = f"{role_info['repo_name']}/AGENTS.md"
    runtime_policy_ref = f"{role_info['repo_name']}/.github/copilot-instructions.md"

    context_ref = (
        f"{catalog['registry_source']}|{catalog['registry_version']}|{role_info['slug']}"
        f"|{agents_ref}@{agents_sha256}|{runtime_policy_ref}@{runtime_policy_sha256}"
    )
    context_sha256 = _sha256_text(context_ref)

    return {
        "instruction_context": {
            "role_slug": role_info["slug"],
            "role_display_name": role_info["display_name"],
            "registry_source": catalog["registry_source"],
            "registry_version": catalog["registry_version"],
            "context_ref": context_ref,
            "context_sha256": context_sha256,
            "agents_excerpt": agents_excerpt,
            "runtime_policy_excerpt": runtime_policy_excerpt,
            "runtime_policy_ref": runtime_policy_ref,
            "runtime_policy_sha256": runtime_policy_sha256,
        }
    }


mcp = FastMCP(
    "joshgpt-dispatcher",
    host=JOSHGPT_DISPATCHER_BIND_HOST,
    port=JOSHGPT_DISPATCHER_BIND_PORT,
)


@mcp.tool()
def dispatch_role_task(
    payload: dict[str, Any],
    shared_token: str = "",
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create queued role task with explicit supervisor assignment."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload=payload,
        correlation=correlation,
        ctx=ctx,
    )
    try:
        _assert_shared_token(shared_token)

        worker_role_slug = _require_non_empty(
            str(payload.get("worker_role_slug", "")),
            "worker_role_slug",
        )
        supervisor_role_slug = _require_non_empty(
            str(payload.get("supervisor_role_slug", "")),
            "supervisor_role_slug",
        )
        objective = _require_non_empty(str(payload.get("objective", "")), "objective")
        constraints = _ensure_list_of_strings(payload.get("constraints"), "constraints")
        input_refs = _ensure_list_of_strings(payload.get("input_refs"), "input_refs")

        task_id = str(payload.get("task_id") or uuid.uuid4())
        created_at = _now_iso()

        with _db() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id,
                    worker_role_slug,
                    supervisor_role_slug,
                    objective,
                    status,
                    constraints_json,
                    input_refs_json,
                    payload_json,
                    created_at,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    task_id,
                    worker_role_slug,
                    supervisor_role_slug,
                    objective,
                    _json_dumps(constraints),
                    _json_dumps(input_refs),
                    _json_dumps(payload),
                    created_at,
                ),
            )
            _record_event(
                conn,
                task_id=task_id,
                event_type="task_dispatched",
                actor_role_slug=None,
                payload={
                    "worker_role_slug": worker_role_slug,
                    "supervisor_role_slug": supervisor_role_slug,
                    "objective": objective,
                },
            )

        response = {
            "task_id": task_id,
            "status": "queued",
            "worker_role_slug": worker_role_slug,
            "supervisor_role_slug": supervisor_role_slug,
            "objective": objective,
            "constraints": constraints,
            "input_refs": input_refs,
            "created_at": created_at,
        }
        _emit_dispatcher_event(
            event="dispatcher.task_dispatched",
            message="Dispatcher task created.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="task_dispatch",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={
                "task_id": task_id,
                "worker_role_slug": worker_role_slug,
                "supervisor_role_slug": supervisor_role_slug,
            },
        )
        return response
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.task_dispatched",
            message="Dispatcher task creation failed.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="task_dispatch",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={
                "objective_chars": len(str(payload.get("objective", ""))),
                "error": str(exc),
            },
        )
        raise


@mcp.tool()
def claim_next_task(
    role_slug: str,
    shared_token: str = "",
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Claim the oldest queued task for the given worker role."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={"role_slug": role_slug},
        correlation=correlation,
        ctx=ctx,
    )
    try:
        _assert_shared_token(shared_token)

        role = _require_non_empty(role_slug, "role_slug")

        with _db() as conn:
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE worker_role_slug = ?
                  AND status = 'queued'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (role,),
            ).fetchone()

            if row is None:
                response = {
                    "claimed": False,
                    "role_slug": role,
                    "task": None,
                }
                _emit_dispatcher_event(
                    event="dispatcher.task_claimed",
                    message="No queued task available for role.",
                    correlation=resolved_correlation,
                    correlation_source=correlation_source,
                    operation="task_claim",
                    status="empty",
                    duration_ms=_duration_ms(started_at),
                    attrs={"role_slug": role},
                )
                return response

            task_id = row["task_id"]
            claimed_at = _now_iso()
            conn.execute(
                "UPDATE tasks SET status = 'claimed', started_at = ? WHERE task_id = ?",
                (claimed_at, task_id),
            )
            _record_event(
                conn,
                task_id=task_id,
                event_type="task_claimed",
                actor_role_slug=role,
                payload={"started_at": claimed_at},
            )

            updated = _fetch_task(conn, task_id)
            response = {
                "claimed": True,
                "role_slug": role,
                "task": _task_row_to_dict(updated),
            }
            _emit_dispatcher_event(
                event="dispatcher.task_claimed",
                message="Queued task claimed.",
                correlation=resolved_correlation,
                correlation_source=correlation_source,
                operation="task_claim",
                status="ok",
                duration_ms=_duration_ms(started_at),
                attrs={"task_id": task_id, "role_slug": role},
            )
            return response
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.task_claimed",
            message="Task claim failed.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="task_claim",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={"role_slug": str(role_slug), "error": str(exc)},
        )
        raise


@mcp.tool()
def set_task_status(
    task_id: str,
    status: str,
    actor_role_slug: str,
    note: str = "",
    shared_token: str = "",
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Set task status with transition event."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={
            "task_id": task_id,
            "status": status,
            "actor_role_slug": actor_role_slug,
        },
        correlation=correlation,
        ctx=ctx,
    )
    try:
        _assert_shared_token(shared_token)

        resolved_task_id = _require_non_empty(task_id, "task_id")
        resolved_status = _ensure_status(status)
        resolved_actor = _require_non_empty(actor_role_slug, "actor_role_slug")

        finished_at = _now_iso() if resolved_status in {"completed", "failed", "canceled"} else None

        with _db() as conn:
            _fetch_task(conn, resolved_task_id)
            conn.execute(
                "UPDATE tasks SET status = ?, finished_at = COALESCE(?, finished_at) WHERE task_id = ?",
                (resolved_status, finished_at, resolved_task_id),
            )
            _record_event(
                conn,
                task_id=resolved_task_id,
                event_type="task_status_changed",
                actor_role_slug=resolved_actor,
                payload={
                    "status": resolved_status,
                    "note": note,
                    "finished_at": finished_at,
                },
            )

            updated = _fetch_task(conn, resolved_task_id)

        response = {
            "task": _task_row_to_dict(updated),
            "status_updated": True,
        }
        _emit_dispatcher_event(
            event="dispatcher.task_status_changed",
            message="Task status updated.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="task_status_update",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={
                "task_id": resolved_task_id,
                "actor_role_slug": resolved_actor,
                "status": resolved_status,
            },
        )
        return response
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.task_status_changed",
            message="Task status update failed.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="task_status_update",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={
                "task_id": str(task_id),
                "status": str(status),
                "actor_role_slug": str(actor_role_slug),
                "error": str(exc),
            },
        )
        raise


@mcp.tool()
def submit_supervisor_question(
    task_id: str,
    from_role_slug: str,
    to_supervisor_role_slug: str,
    escalation_reason: str,
    question: str,
    role_context_ref: str,
    role_context_sha256: str,
    shared_token: str = "",
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Submit supervisor question for a task and mark task awaiting supervisor."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={
            "task_id": task_id,
            "from_role_slug": from_role_slug,
            "to_supervisor_role_slug": to_supervisor_role_slug,
            "escalation_reason": escalation_reason,
            "correlation": correlation or {},
        },
        correlation=correlation,
        ctx=ctx,
    )
    try:
        _assert_shared_token(shared_token)

        resolved_task_id = _require_non_empty(task_id, "task_id")
        from_role = _require_non_empty(from_role_slug, "from_role_slug")
        to_role = _require_non_empty(to_supervisor_role_slug, "to_supervisor_role_slug")
        resolved_reason = _require_non_empty(escalation_reason, "escalation_reason")
        resolved_question = _require_non_empty(question, "question")
        context_ref = _require_non_empty(role_context_ref, "role_context_ref")
        context_hash = _require_non_empty(role_context_sha256, "role_context_sha256")

        message_id = str(uuid.uuid4())
        created_at = _now_iso()

        with _db() as conn:
            task = _fetch_task(conn, resolved_task_id)
            expected_supervisor = task["supervisor_role_slug"]
            if to_role != expected_supervisor:
                raise ValueError(
                    f"to_supervisor_role_slug must match task.supervisor_role_slug={expected_supervisor!r}"
                )

            conn.execute(
                """
                INSERT INTO supervisor_messages (
                    message_id,
                    task_id,
                    from_role_slug,
                    to_role_slug,
                    escalation_reason,
                    question,
                    role_context_ref,
                    role_context_hash,
                    status,
                    response_json,
                    created_at,
                    responded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
                """,
                (
                    message_id,
                    resolved_task_id,
                    from_role,
                    to_role,
                    resolved_reason,
                    resolved_question,
                    context_ref,
                    context_hash,
                    created_at,
                ),
            )

            conn.execute(
                "UPDATE tasks SET status = 'awaiting_supervisor' WHERE task_id = ?",
                (resolved_task_id,),
            )

            _record_event(
                conn,
                task_id=resolved_task_id,
                event_type="supervisor_question_submitted",
                actor_role_slug=from_role,
                payload={
                    "message_id": message_id,
                    "to_supervisor_role_slug": to_role,
                    "escalation_reason": resolved_reason,
                    "role_context_ref": context_ref,
                    "role_context_sha256": context_hash,
                },
            )

        response = {
            "message_id": message_id,
            "task_id": resolved_task_id,
            "status": "pending",
            "to_supervisor_role_slug": to_role,
            "created_at": created_at,
        }
        _emit_dispatcher_event(
            event="dispatcher.supervisor_question_submitted",
            message="Supervisor question submitted.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="supervisor_question_submit",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={
                "task_id": resolved_task_id,
                "message_id": message_id,
                "from_role_slug": from_role,
                "to_supervisor_role_slug": to_role,
            },
        )
        return response
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.supervisor_question_submitted",
            message="Supervisor question submission failed.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="supervisor_question_submit",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={
                "task_id": str(task_id),
                "from_role_slug": str(from_role_slug),
                "to_supervisor_role_slug": str(to_supervisor_role_slug),
                "error": str(exc),
            },
        )
        raise


@mcp.tool()
def list_pending_supervisor_questions(
    role_slug: str,
    shared_token: str = "",
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List pending supervisor questions assigned to the given role."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={"role_slug": role_slug},
        correlation=correlation,
        ctx=ctx,
    )
    try:
        _assert_shared_token(shared_token)

        role = _require_non_empty(role_slug, "role_slug")

        with _db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM supervisor_messages
                WHERE to_role_slug = ?
                  AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (role,),
            ).fetchall()

        messages: list[dict[str, Any]] = []
        for row in rows:
            messages.append(
                {
                    "message_id": row["message_id"],
                    "task_id": row["task_id"],
                    "from_role_slug": row["from_role_slug"],
                    "to_role_slug": row["to_role_slug"],
                    "escalation_reason": row["escalation_reason"],
                    "question": row["question"],
                    "role_context_ref": row["role_context_ref"],
                    "role_context_sha256": row["role_context_hash"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
            )

        response = {
            "role_slug": role,
            "pending_count": len(messages),
            "messages": messages,
        }
        _emit_dispatcher_event(
            event="dispatcher.supervisor_questions_listed",
            message="Pending supervisor questions listed.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="supervisor_question_list",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={"role_slug": role, "pending_count": len(messages)},
        )
        return response
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.supervisor_questions_listed",
            message="Failed to list pending supervisor questions.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="supervisor_question_list",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={"role_slug": str(role_slug), "error": str(exc)},
        )
        raise


@mcp.tool()
def respond_supervisor_question(
    message_id: str,
    supervisor_role_slug: str,
    decision_payload: dict[str, Any],
    shared_token: str = "",
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Attach supervisor decision payload to a pending supervisor question."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={
            "message_id": message_id,
            "supervisor_role_slug": supervisor_role_slug,
            "decision_payload": decision_payload,
            "correlation": correlation or {},
        },
        correlation=correlation,
        ctx=ctx,
    )
    try:
        _assert_shared_token(shared_token)

        resolved_message_id = _require_non_empty(message_id, "message_id")
        resolved_supervisor = _require_non_empty(supervisor_role_slug, "supervisor_role_slug")

        responded_at = _now_iso()

        with _db() as conn:
            message = conn.execute(
                "SELECT * FROM supervisor_messages WHERE message_id = ?",
                (resolved_message_id,),
            ).fetchone()
            if message is None:
                raise LookupError(f"supervisor message not found: {resolved_message_id}")

            if message["status"] != "pending":
                raise ValueError(
                    f"supervisor message is not pending (current status: {message['status']})"
                )
            if message["to_role_slug"] != resolved_supervisor:
                raise ValueError(
                    "supervisor_role_slug does not match message assignment"
                )

            task_id = message["task_id"]

            conn.execute(
                """
                UPDATE supervisor_messages
                SET status = 'answered', response_json = ?, responded_at = ?
                WHERE message_id = ?
                """,
                (_json_dumps(decision_payload), responded_at, resolved_message_id),
            )

            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE task_id = ?",
                (task_id,),
            )

            _record_event(
                conn,
                task_id=task_id,
                event_type="supervisor_question_answered",
                actor_role_slug=resolved_supervisor,
                payload={
                    "message_id": resolved_message_id,
                    "decision_payload": decision_payload,
                },
            )

        response = {
            "message_id": resolved_message_id,
            "task_id": task_id,
            "status": "answered",
            "responded_at": responded_at,
        }
        _emit_dispatcher_event(
            event="dispatcher.supervisor_question_answered",
            message="Supervisor decision recorded.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="supervisor_question_respond",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={
                "task_id": task_id,
                "message_id": resolved_message_id,
                "supervisor_role_slug": resolved_supervisor,
            },
        )
        return response
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.supervisor_question_answered",
            message="Failed to record supervisor decision.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="supervisor_question_respond",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={
                "message_id": str(message_id),
                "supervisor_role_slug": str(supervisor_role_slug),
                "error": str(exc),
            },
        )
        raise


@mcp.tool()
def get_task_status(
    task_id: str,
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return task details, events, and related supervisor messages."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={"task_id": task_id},
        correlation=correlation,
        ctx=ctx,
    )
    try:
        resolved_task_id = _require_non_empty(task_id, "task_id")

        with _db() as conn:
            task = _fetch_task(conn, resolved_task_id)

            event_rows = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY event_id ASC",
                (resolved_task_id,),
            ).fetchall()

            message_rows = conn.execute(
                "SELECT * FROM supervisor_messages WHERE task_id = ? ORDER BY created_at ASC",
                (resolved_task_id,),
            ).fetchall()

        events: list[dict[str, Any]] = []
        for row in event_rows:
            events.append(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "actor_role_slug": row["actor_role_slug"],
                    "payload": _json_loads(row["payload_json"], {}),
                    "created_at": row["created_at"],
                }
            )

        messages: list[dict[str, Any]] = []
        for row in message_rows:
            messages.append(
                {
                    "message_id": row["message_id"],
                    "from_role_slug": row["from_role_slug"],
                    "to_role_slug": row["to_role_slug"],
                    "escalation_reason": row["escalation_reason"],
                    "question": row["question"],
                    "role_context_ref": row["role_context_ref"],
                    "role_context_sha256": row["role_context_hash"],
                    "status": row["status"],
                    "response": _json_loads(row["response_json"], None),
                    "created_at": row["created_at"],
                    "responded_at": row["responded_at"],
                }
            )

        response = {
            "task": _task_row_to_dict(task),
            "events": events,
            "supervisor_messages": messages,
        }
        _emit_dispatcher_event(
            event="dispatcher.task_status_fetched",
            message="Task status fetched.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="task_status_get",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={
                "task_id": resolved_task_id,
                "event_count": len(events),
                "supervisor_message_count": len(messages),
            },
        )
        return response
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.task_status_fetched",
            message="Failed to fetch task status.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="task_status_get",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={"task_id": str(task_id), "error": str(exc)},
        )
        raise


@mcp.tool()
def list_role_queues(
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return queued/claimed/running counts grouped by worker role."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={},
        correlation=correlation,
        ctx=ctx,
    )
    try:
        with _db() as conn:
            rows = conn.execute(
                """
                SELECT worker_role_slug, status, COUNT(*) AS count
                FROM tasks
                GROUP BY worker_role_slug, status
                ORDER BY worker_role_slug, status
                """
            ).fetchall()

        by_role: dict[str, dict[str, int]] = {}
        for row in rows:
            role = row["worker_role_slug"]
            status = row["status"]
            count = int(row["count"])
            by_role.setdefault(role, {})[status] = count

        response = {
            "db_path": str(JOSHGPT_DISPATCHER_DB_PATH),
            "queues": by_role,
        }
        _emit_dispatcher_event(
            event="dispatcher.role_queues_listed",
            message="Role queue summary listed.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="role_queues_list",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={"roles": len(by_role)},
        )
        return response
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.role_queues_listed",
            message="Failed to list role queues.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="role_queues_list",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={"error": str(exc)},
        )
        raise


@mcp.tool()
def list_role_catalog(
    shared_token: str = "",
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return registry-backed role catalog for supervisor assignment."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={},
        correlation=correlation,
        ctx=ctx,
    )
    try:
        _assert_shared_token(shared_token)
        catalog = _load_role_catalog()
        _emit_dispatcher_event(
            event="dispatcher.role_catalog_loaded",
            message="Role catalog loaded.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="role_catalog_list",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={
                "registry_source": catalog.get("registry_source", ""),
                "registry_version": catalog.get("registry_version", ""),
                "role_count": len(catalog.get("roles", [])),
            },
        )
        return catalog
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.role_catalog_loaded",
            message="Failed to load role catalog.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="role_catalog_list",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={"error": str(exc)},
        )
        raise


@mcp.tool()
def get_supervisor_role_context(
    role_slug: str,
    shared_token: str = "",
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return bounded instruction context for selected supervisor role."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={"role_slug": role_slug},
        correlation=correlation,
        ctx=ctx,
    )
    try:
        _assert_shared_token(shared_token)
        context_payload = _resolve_supervisor_role_context(role_slug)
        instruction = context_payload.get("instruction_context", {})
        _emit_dispatcher_event(
            event="dispatcher.supervisor_context_loaded",
            message="Supervisor role context loaded.",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="supervisor_context_get",
            status="ok",
            duration_ms=_duration_ms(started_at),
            attrs={
                "role_slug": str(instruction.get("role_slug", "")),
                "context_ref": str(instruction.get("context_ref", "")),
            },
        )
        return context_payload
    except Exception as exc:
        _emit_dispatcher_event(
            event="dispatcher.supervisor_context_loaded",
            message="Failed to load supervisor role context.",
            level="error",
            correlation=resolved_correlation,
            correlation_source=correlation_source,
            operation="supervisor_context_get",
            status="error",
            duration_ms=_duration_ms(started_at),
            error_code=_error_code(exc),
            attrs={"role_slug": str(role_slug), "error": str(exc)},
        )
        raise


@mcp.tool()
def dispatcher_info(
    correlation: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Expose dispatcher runtime config summary."""

    started_at = time.time()
    resolved_correlation, correlation_source = _resolve_call_correlation(
        payload={},
        correlation=correlation,
        ctx=ctx,
    )
    info = {
        "name": "joshgpt-dispatcher",
        "transport": JOSHGPT_DISPATCHER_TRANSPORT,
        "bind_host": JOSHGPT_DISPATCHER_BIND_HOST,
        "bind_port": JOSHGPT_DISPATCHER_BIND_PORT,
        "db_path": str(JOSHGPT_DISPATCHER_DB_PATH),
        "require_shared_token": JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN,
        "role_registry_path": str(JOSHGPT_ROLE_REGISTRY_PATH),
        "role_repos_base_path": str(JOSHGPT_ROLE_REPOS_BASE_PATH),
        "supervisor_context_max_chars": JOSHGPT_SUPERVISOR_CONTEXT_MAX_CHARS,
        "statuses": sorted(ALLOWED_TASK_STATUSES),
    }
    _emit_dispatcher_event(
        event="dispatcher.info",
        message="Dispatcher runtime info requested.",
        correlation=resolved_correlation,
        correlation_source=correlation_source,
        operation="dispatcher_info",
        status="ok",
        duration_ms=_duration_ms(started_at),
        attrs={
            "transport": JOSHGPT_DISPATCHER_TRANSPORT,
            "bind_port": JOSHGPT_DISPATCHER_BIND_PORT,
        },
    )
    return info


if __name__ == "__main__":
    _init_db()
    _emit_dispatcher_event(
        event="dispatcher.startup",
        message="Dispatcher service starting.",
        correlation={"chat_session_id": "unknown", "turn_id": "unknown", "request_id": "", "tool_call_id": ""},
        correlation_source="unknown",
        operation="startup",
        status="ok",
        attrs={
            "transport": JOSHGPT_DISPATCHER_TRANSPORT,
            "bind_host": JOSHGPT_DISPATCHER_BIND_HOST,
            "bind_port": JOSHGPT_DISPATCHER_BIND_PORT,
            "db_path": str(JOSHGPT_DISPATCHER_DB_PATH),
        },
    )
    print(
        (
            "Starting joshgpt-dispatcher with "
            f"transport={JOSHGPT_DISPATCHER_TRANSPORT} "
            f"host={JOSHGPT_DISPATCHER_BIND_HOST} "
            f"port={JOSHGPT_DISPATCHER_BIND_PORT} "
            f"db_path={JOSHGPT_DISPATCHER_DB_PATH} "
            f"role_registry_path={JOSHGPT_ROLE_REGISTRY_PATH} "
            f"role_repos_base_path={JOSHGPT_ROLE_REPOS_BASE_PATH} "
            f"require_shared_token={JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN}"
        ),
        flush=True,
    )
    mcp.run(transport=JOSHGPT_DISPATCHER_TRANSPORT)
