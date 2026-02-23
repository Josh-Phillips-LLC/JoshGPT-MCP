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
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from mcp.server.fastmcp import FastMCP

DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8788
DEFAULT_TRANSPORT = "streamable-http"
ALLOWED_TRANSPORTS = {"stdio", "sse", "streamable-http"}

DEFAULT_DB_PATH = "/tmp/joshgpt_dispatcher.db"
DEFAULT_REQUIRE_SHARED_TOKEN = True

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


mcp = FastMCP(
    "joshgpt-dispatcher",
    host=JOSHGPT_DISPATCHER_BIND_HOST,
    port=JOSHGPT_DISPATCHER_BIND_PORT,
)


@mcp.tool()
def dispatch_role_task(payload: dict[str, Any], shared_token: str = "") -> dict[str, Any]:
    """Create queued role task with explicit supervisor assignment."""

    _assert_shared_token(shared_token)

    worker_role_slug = _require_non_empty(str(payload.get("worker_role_slug", "")), "worker_role_slug")
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

    return {
        "task_id": task_id,
        "status": "queued",
        "worker_role_slug": worker_role_slug,
        "supervisor_role_slug": supervisor_role_slug,
        "objective": objective,
        "constraints": constraints,
        "input_refs": input_refs,
        "created_at": created_at,
    }


@mcp.tool()
def claim_next_task(role_slug: str, shared_token: str = "") -> dict[str, Any]:
    """Claim the oldest queued task for the given worker role."""

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
            return {
                "claimed": False,
                "role_slug": role,
                "task": None,
            }

        task_id = row["task_id"]
        started_at = _now_iso()
        conn.execute(
            "UPDATE tasks SET status = 'claimed', started_at = ? WHERE task_id = ?",
            (started_at, task_id),
        )
        _record_event(
            conn,
            task_id=task_id,
            event_type="task_claimed",
            actor_role_slug=role,
            payload={"started_at": started_at},
        )

        updated = _fetch_task(conn, task_id)
        return {
            "claimed": True,
            "role_slug": role,
            "task": _task_row_to_dict(updated),
        }


@mcp.tool()
def set_task_status(
    task_id: str,
    status: str,
    actor_role_slug: str,
    note: str = "",
    shared_token: str = "",
) -> dict[str, Any]:
    """Set task status with transition event."""

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

    return {
        "task": _task_row_to_dict(updated),
        "status_updated": True,
    }


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
) -> dict[str, Any]:
    """Submit supervisor question for a task and mark task awaiting supervisor."""

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

    return {
        "message_id": message_id,
        "task_id": resolved_task_id,
        "status": "pending",
        "to_supervisor_role_slug": to_role,
        "created_at": created_at,
    }


@mcp.tool()
def list_pending_supervisor_questions(role_slug: str, shared_token: str = "") -> dict[str, Any]:
    """List pending supervisor questions assigned to the given role."""

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

    return {
        "role_slug": role,
        "pending_count": len(messages),
        "messages": messages,
    }


@mcp.tool()
def respond_supervisor_question(
    message_id: str,
    supervisor_role_slug: str,
    decision_payload: dict[str, Any],
    shared_token: str = "",
) -> dict[str, Any]:
    """Attach supervisor decision payload to a pending supervisor question."""

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

    return {
        "message_id": resolved_message_id,
        "task_id": task_id,
        "status": "answered",
        "responded_at": responded_at,
    }


@mcp.tool()
def get_task_status(task_id: str) -> dict[str, Any]:
    """Return task details, events, and related supervisor messages."""

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

    return {
        "task": _task_row_to_dict(task),
        "events": events,
        "supervisor_messages": messages,
    }


@mcp.tool()
def list_role_queues() -> dict[str, Any]:
    """Return queued/claimed/running counts grouped by worker role."""

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

    return {
        "db_path": str(JOSHGPT_DISPATCHER_DB_PATH),
        "queues": by_role,
    }


@mcp.tool()
def dispatcher_info() -> dict[str, Any]:
    """Expose dispatcher runtime config summary."""

    return {
        "name": "joshgpt-dispatcher",
        "transport": JOSHGPT_DISPATCHER_TRANSPORT,
        "bind_host": JOSHGPT_DISPATCHER_BIND_HOST,
        "bind_port": JOSHGPT_DISPATCHER_BIND_PORT,
        "db_path": str(JOSHGPT_DISPATCHER_DB_PATH),
        "require_shared_token": JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN,
        "statuses": sorted(ALLOWED_TASK_STATUSES),
    }


if __name__ == "__main__":
    _init_db()
    print(
        (
            "Starting joshgpt-dispatcher with "
            f"transport={JOSHGPT_DISPATCHER_TRANSPORT} "
            f"host={JOSHGPT_DISPATCHER_BIND_HOST} "
            f"port={JOSHGPT_DISPATCHER_BIND_PORT} "
            f"db_path={JOSHGPT_DISPATCHER_DB_PATH} "
            f"require_shared_token={JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN}"
        ),
        flush=True,
    )
    mcp.run(transport=JOSHGPT_DISPATCHER_TRANSPORT)
