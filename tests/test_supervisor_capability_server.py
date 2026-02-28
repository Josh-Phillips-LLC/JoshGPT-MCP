#!/usr/bin/env python3
"""Supervisor capability fail-safe behavior tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Keep token checks disabled in unit tests to focus on decision behavior.
os.environ.setdefault("JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN", "false")

import supervisor_capability_server as server  # noqa: E402

VALID_REQUEST_PAYLOAD = {
    "mission_id": "mission-123",
    "goal": "Resolve worker blocker with supervisor decision",
    "current_phase": "validation",
    "blocked_reason": "insufficient_context",
    "attempt_history": ["Checked logs", "Retried workflow"],
    "evidence_summary": "Runtime logs were inconclusive.",
    "constraints": ["read-only workspace"],
    "authorized_scope": {
        "scope_id": "scope-abc",
        "targets": ["workspace"],
        "expires_utc": "2026-02-27T23:59:59Z",
    },
    "requested_decision": "next_step",
}

VALID_RESPONSE_PAYLOAD = {
    "decision": "proceed",
    "rationale": "Evidence is sufficient to continue.",
    "next_actions": ["Continue validation with collected evidence."],
    "confidence": 0.81,
    "safety_checks": ["scope_ok"],
    "audit_tags": ["provider-codex-cli"],
}


class SupervisorCapabilityServerTests(unittest.TestCase):
    def test_valid_canonical_request_returns_schema_valid_response(self) -> None:
        with mock.patch.object(server, "_call_codex_cli", return_value=VALID_RESPONSE_PAYLOAD):
            response = server.ask_codex_supervisor(VALID_REQUEST_PAYLOAD, shared_token="")

        server.RESPONSE_VALIDATOR.validate(response)
        self.assertEqual(response["decision"], "proceed")

    def test_missing_codex_auth_returns_pause_for_human(self) -> None:
        with mock.patch.object(
            server,
            "_call_codex_cli",
            side_effect=RuntimeError("codex login status failed"),
        ):
            response = server.ask_codex_supervisor(VALID_REQUEST_PAYLOAD, shared_token="")

        server.RESPONSE_VALIDATOR.validate(response)
        self.assertEqual(response["decision"], "pause_for_human")

    def test_malformed_codex_output_returns_pause_for_human(self) -> None:
        malformed_response = {"decision": "proceed"}
        with mock.patch.object(server, "_call_codex_cli", return_value=malformed_response):
            response = server.ask_codex_supervisor(VALID_REQUEST_PAYLOAD, shared_token="")

        server.RESPONSE_VALIDATOR.validate(response)
        self.assertEqual(response["decision"], "pause_for_human")

    def test_timeout_returns_pause_for_human(self) -> None:
        timeout_error = subprocess.TimeoutExpired(cmd=["codex", "exec"], timeout=1)
        with mock.patch.object(server, "_call_codex_cli", side_effect=timeout_error):
            response = server.ask_codex_supervisor(VALID_REQUEST_PAYLOAD, shared_token="")

        server.RESPONSE_VALIDATOR.validate(response)
        self.assertEqual(response["decision"], "pause_for_human")

    def test_json_decode_error_returns_pause_for_human(self) -> None:
        with mock.patch.object(
            server,
            "_call_codex_cli",
            side_effect=json.JSONDecodeError("bad json", "}", 0),
        ):
            response = server.ask_codex_supervisor(VALID_REQUEST_PAYLOAD, shared_token="")

        server.RESPONSE_VALIDATOR.validate(response)
        self.assertEqual(response["decision"], "pause_for_human")


if __name__ == "__main__":
    unittest.main()
