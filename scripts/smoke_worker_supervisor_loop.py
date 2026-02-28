#!/usr/bin/env python3
"""End-to-end worker/supervisor escalation smoke test using MCP calls only.

Flow:
1. Dispatch task to worker role with explicit supervisor role assignment.
2. Worker role claims task.
3. Worker submits supervisor question.
4. Supervisor role lists pending questions.
5. Codex supervisor capability returns canonical decision.
6. Dispatcher records supervisor response.
7. Fetch final task status/events for verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from typing import Any

try:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ModuleNotFoundError as exc:
    print(
        (
            f"Missing dependency: {exc.name}. "
            "Install runtime dependencies first (`pip install -r requirements.txt`) "
            "or run with the project venv "
            "(`.venv/bin/python scripts/smoke_worker_supervisor_loop.py`)."
        ),
        file=sys.stderr,
    )
    raise SystemExit(1)


def _parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _extract_tool_payload(result: Any) -> Any:
    if getattr(result, "isError", False):
        structured = getattr(result, "structuredContent", None)
        if structured:
            raise RuntimeError(f"MCP tool call failed: {json.dumps(structured, indent=2)}")
        text_chunks = []
        for item in getattr(result, "content", []):
            text = getattr(item, "text", "")
            if text:
                text_chunks.append(text)
        joined = "\n".join(text_chunks).strip() or "<no error details>"
        raise RuntimeError(f"MCP tool call failed: {joined}")

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    for item in getattr(result, "content", []):
        text = getattr(item, "text", "")
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


class McpToolClient:
    def __init__(self, endpoint_url: str) -> None:
        self._endpoint_url = endpoint_url
        self._http_context = None
        self._session_context = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpToolClient":
        self._http_context = streamablehttp_client(self._endpoint_url)
        read_stream, write_stream, _ = await self._http_context.__aenter__()
        self._session_context = ClientSession(read_stream, write_stream)
        self._session = await self._session_context.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(exc_type, exc, tb)
        if self._http_context is not None:
            await self._http_context.__aexit__(exc_type, exc, tb)

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("MCP session is not initialized")
        result = await self._session.call_tool(name, arguments=arguments)
        return _extract_tool_payload(result)


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    dispatcher_token = args.dispatcher_shared_token
    supervisor_token = args.supervisor_shared_token

    task_constraints = _parse_csv(args.task_constraints)
    task_input_refs = _parse_csv(args.task_input_refs)
    attempt_history = _parse_csv(args.attempt_history)
    scope_targets = _parse_csv(args.authorized_targets)

    role_context_ref = args.role_context_ref
    role_context_sha256 = (
        args.role_context_sha256
        if args.role_context_sha256
        else hashlib.sha256(role_context_ref.encode("utf-8")).hexdigest()
    )

    async with McpToolClient(args.dispatcher_url) as dispatcher:
        dispatched = await dispatcher.call(
            "dispatch_role_task",
            {
                "payload": {
                    "worker_role_slug": args.worker_role_slug,
                    "supervisor_role_slug": args.supervisor_role_slug,
                    "objective": args.objective,
                    "constraints": task_constraints,
                    "input_refs": task_input_refs,
                },
                "shared_token": dispatcher_token,
            },
        )

        task_id = dispatched["task_id"]

        claimed = await dispatcher.call(
            "claim_next_task",
            {
                "role_slug": args.worker_role_slug,
                "shared_token": dispatcher_token,
            },
        )

        question = await dispatcher.call(
            "submit_supervisor_question",
            {
                "task_id": task_id,
                "from_role_slug": args.worker_role_slug,
                "to_supervisor_role_slug": args.supervisor_role_slug,
                "escalation_reason": args.dispatcher_escalation_reason,
                "question": args.question,
                "role_context_ref": role_context_ref,
                "role_context_sha256": role_context_sha256,
                "shared_token": dispatcher_token,
            },
        )

        pending = await dispatcher.call(
            "list_pending_supervisor_questions",
            {
                "role_slug": args.supervisor_role_slug,
                "shared_token": dispatcher_token,
            },
        )

    request_payload: dict[str, Any] = {
        "mission_id": task_id,
        "goal": args.objective,
        "current_phase": args.current_phase,
        "blocked_reason": args.blocked_reason,
        "attempt_history": attempt_history,
        "constraints": task_constraints,
        "authorized_scope": {
            "scope_id": args.authorized_scope_id,
            "targets": scope_targets,
            "expires_utc": args.authorized_expires_utc,
        },
        "requested_decision": args.requested_decision,
    }
    if args.evidence_summary.strip():
        request_payload["evidence_summary"] = args.evidence_summary.strip()

    async with McpToolClient(args.supervisor_url) as supervisor:
        decision = await supervisor.call(
            "ask_codex_supervisor",
            {
                "payload": request_payload,
                "shared_token": supervisor_token,
            },
        )

    async with McpToolClient(args.dispatcher_url) as dispatcher:
        responded = await dispatcher.call(
            "respond_supervisor_question",
            {
                "message_id": question["message_id"],
                "supervisor_role_slug": args.supervisor_role_slug,
                "decision_payload": decision,
                "shared_token": dispatcher_token,
            },
        )

        task_status = await dispatcher.call(
            "get_task_status",
            {"task_id": task_id},
        )

    return {
        "dispatcher_url": args.dispatcher_url,
        "supervisor_url": args.supervisor_url,
        "dispatched": dispatched,
        "claimed": claimed,
        "question": question,
        "pending_for_supervisor": pending,
        "supervisor_request": request_payload,
        "supervisor_decision": decision,
        "responded": responded,
        "task_status": task_status,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run end-to-end worker/supervisor loop via dispatcher and supervisor MCP endpoints."
    )
    parser.add_argument(
        "--dispatcher-url",
        default="http://127.0.0.1:8788/mcp",
        help="Dispatcher MCP endpoint URL",
    )
    parser.add_argument(
        "--supervisor-url",
        default="http://127.0.0.1:8789/mcp",
        help="Supervisor MCP endpoint URL",
    )
    parser.add_argument(
        "--dispatcher-shared-token",
        default="replace-me-dispatcher-token",
        help="Shared token for dispatcher tools",
    )
    parser.add_argument(
        "--supervisor-shared-token",
        default="replace-me-supervisor-token",
        help="Shared token for supervisor capability tool",
    )
    parser.add_argument(
        "--worker-role-slug",
        default="implementation-specialist",
        help="Worker role slug",
    )
    parser.add_argument(
        "--supervisor-role-slug",
        default="hr-ai-agent-specialist",
        help="Supervisor role slug",
    )
    parser.add_argument(
        "--objective",
        default="Apply minimal README improvement with governed workflow.",
        help="Task objective",
    )
    parser.add_argument(
        "--task-constraints",
        default="issue-scoped,no protected paths",
        help="Comma-separated task constraints",
    )
    parser.add_argument(
        "--task-input-refs",
        default="issue/5",
        help="Comma-separated task input references",
    )
    parser.add_argument(
        "--dispatcher-escalation-reason",
        default="execution_blocker",
        choices=[
            "instruction_ambiguity",
            "missing_required_input",
            "authority_conflict",
            "quality_risk",
            "execution_blocker",
            "other",
        ],
        help="Escalation reason stored in dispatcher supervisor question",
    )
    parser.add_argument(
        "--question",
        default="Need supervisor decision on next best action for this blocked objective.",
        help="Supervisor question text stored in dispatcher",
    )
    parser.add_argument(
        "--current-phase",
        default="validation",
        choices=["discovery", "classification", "validation", "reporting"],
        help="Current mission phase for ask_codex_supervisor request",
    )
    parser.add_argument(
        "--blocked-reason",
        default="ambiguous_result",
        choices=[
            "tool_error",
            "ambiguous_result",
            "policy_conflict",
            "insufficient_context",
            "repeated_failure",
        ],
        help="Blocked reason for ask_codex_supervisor request",
    )
    parser.add_argument(
        "--attempt-history",
        default="initial-run produced ambiguous output",
        help="Comma-separated attempt history entries",
    )
    parser.add_argument(
        "--evidence-summary",
        default="Tool output did not converge on a single safe next step.",
        help="Optional evidence summary for ask_codex_supervisor request",
    )
    parser.add_argument(
        "--authorized-scope-id",
        default="scope-supervised-smoke",
        help="Authorized scope ID",
    )
    parser.add_argument(
        "--authorized-targets",
        default="workspace",
        help="Comma-separated authorized scope targets",
    )
    parser.add_argument(
        "--authorized-expires-utc",
        default="2099-12-31T23:59:59Z",
        help="Authorized scope expiration in UTC ISO8601 format",
    )
    parser.add_argument(
        "--requested-decision",
        default="next_step",
        choices=["next_step", "prioritize", "deconflict", "stop_or_continue"],
        help="Requested decision category for ask_codex_supervisor request",
    )
    parser.add_argument(
        "--role-context-ref",
        default="AGENTS.md#implementation-specialist",
        help="Reference pointer to role context source for dispatcher record",
    )
    parser.add_argument(
        "--role-context-sha256",
        default="",
        help="Optional explicit role context SHA256 (auto-generated from ref if omitted)",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print concise key=value summary instead of full JSON payload.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = anyio.run(run_smoke, args)
    if args.summary_only:
        print(f"task_id={output['dispatched']['task_id']}")
        print(f"message_id={output['question']['message_id']}")
        print(f"decision={output['supervisor_decision']['decision']}")
        print(f"status={output['task_status']['task']['status']}")
        print(
            "flow_passed="
            + ("yes" if output["responded"]["status"] == "answered" else "no")
        )
        return
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
