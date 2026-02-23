#!/usr/bin/env python3
"""End-to-end worker/supervisor escalation smoke test using MCP calls only.

Flow:
1. Dispatch task to worker role with explicit supervisor role assignment.
2. Worker role claims task.
3. Worker submits supervisor question.
4. Supervisor role lists pending questions.
5. Supervisor capability returns deterministic decision.
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

    constraints = _parse_csv(args.task_constraints)
    input_refs = _parse_csv(args.task_input_refs)
    role_constraints = _parse_csv(args.role_constraints)
    authority_boundaries = _parse_csv(args.role_authority_boundaries)
    quality_standards = _parse_csv(args.role_quality_standards)

    role_context_ref = args.role_context_ref
    role_context_sha256 = (
        args.role_context_sha256
        if args.role_context_sha256
        else hashlib.sha256(role_context_ref.encode("utf-8")).hexdigest()
    )

    request_id = str(uuid.uuid4())

    async with McpToolClient(args.dispatcher_url) as dispatcher:
        dispatched = await dispatcher.call(
            "dispatch_role_task",
            {
                "payload": {
                    "worker_role_slug": args.worker_role_slug,
                    "supervisor_role_slug": args.supervisor_role_slug,
                    "objective": args.objective,
                    "constraints": constraints,
                    "input_refs": input_refs,
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
                "escalation_reason": args.escalation_reason,
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

    async with McpToolClient(args.supervisor_url) as supervisor:
        decision = await supervisor.call(
            "ask_supervisor_capability",
            {
                "payload": {
                    "request_id": request_id,
                    "task_id": task_id,
                    "worker_role_slug": args.worker_role_slug,
                    "supervisor_role_slug": args.supervisor_role_slug,
                    "requested_decision": args.requested_decision,
                    "escalation_reason": args.escalation_reason,
                    "question": args.question,
                    "role_context": {
                        "role_slug": args.supervisor_role_slug,
                        "role_job_description_ref": role_context_ref,
                        "role_job_description_sha256": role_context_sha256,
                        "authority_boundaries": authority_boundaries,
                        "constraints": role_constraints,
                        "quality_standards": quality_standards,
                    },
                    "task_context": {
                        "objective": args.objective,
                        "constraints": constraints,
                        "input_refs": input_refs,
                    },
                },
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
        "request_id": request_id,
        "dispatcher_url": args.dispatcher_url,
        "supervisor_url": args.supervisor_url,
        "dispatched": dispatched,
        "claimed": claimed,
        "question": question,
        "pending_for_supervisor": pending,
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
        "--escalation-reason",
        default="instruction_ambiguity",
        choices=[
            "instruction_ambiguity",
            "missing_required_input",
            "authority_conflict",
            "quality_risk",
            "execution_blocker",
            "other",
        ],
        help="Escalation reason for supervisor question",
    )
    parser.add_argument(
        "--question",
        default="Should this change use Refs or Closes in PR description?",
        help="Supervisor question text",
    )
    parser.add_argument(
        "--requested-decision",
        default="auto",
        choices=["auto", "allow", "deny", "clarify", "escalate"],
        help="Decision hint sent to supervisor capability",
    )
    parser.add_argument(
        "--role-context-ref",
        default="AGENTS.md#hr-ai-agent-specialist",
        help="Reference pointer to role context source",
    )
    parser.add_argument(
        "--role-context-sha256",
        default="",
        help="Optional explicit role context SHA256 (auto-generated from ref if omitted)",
    )
    parser.add_argument(
        "--role-constraints",
        default="must follow issue scope,must escalate ambiguity",
        help="Comma-separated role constraints injected into supervisor payload",
    )
    parser.add_argument(
        "--role-authority-boundaries",
        default="follow-governance",
        help="Comma-separated authority boundary markers",
    )
    parser.add_argument(
        "--role-quality-standards",
        default="deterministic decisions",
        help="Comma-separated role quality standards",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = anyio.run(run_smoke, args)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
