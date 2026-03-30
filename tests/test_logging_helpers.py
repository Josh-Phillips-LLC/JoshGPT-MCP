#!/usr/bin/env python3
"""Unit tests for shared logging helpers."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import logging_helpers as helpers  # noqa: E402


class LoggingHelpersTests(unittest.TestCase):
    def test_resolve_correlation_prefers_explicit_over_payload_and_headers(self) -> None:
        payload = {
            "correlation": {
                "chat_session_id": "payload-session",
                "turn_id": "payload-turn",
                "request_id": "payload-req",
            }
        }
        headers = {
            "x-chat-session-id": "header-session",
            "x-turn-id": "header-turn",
            "x-request-id": "header-req",
        }
        correlation, source = helpers.resolve_correlation(
            payload,
            correlation={
                "chat_session_id": "explicit-session",
                "turn_id": "explicit-turn",
                "request_id": "explicit-req",
                "tool_call_id": "explicit-tool",
            },
            headers=headers,
        )
        self.assertEqual(source, "explicit")
        self.assertEqual(correlation["chat_session_id"], "explicit-session")
        self.assertEqual(correlation["turn_id"], "explicit-turn")
        self.assertEqual(correlation["request_id"], "explicit-req")
        self.assertEqual(correlation["tool_call_id"], "explicit-tool")

    def test_resolve_correlation_uses_payload_then_headers_then_unknown(self) -> None:
        payload_only, source_payload = helpers.resolve_correlation(
            {"correlation": {"chat_session_id": "payload-session", "turn_id": "payload-turn"}}
        )
        self.assertEqual(source_payload, "payload")
        self.assertEqual(payload_only["chat_session_id"], "payload-session")
        self.assertEqual(payload_only["turn_id"], "payload-turn")

        headers_only, source_headers = helpers.resolve_correlation(
            {},
            headers={
                "x-chat-session-id": "header-session",
                "x-turn-id": "header-turn",
                "x-request-id": "header-req",
            },
        )
        self.assertEqual(source_headers, "headers")
        self.assertEqual(headers_only["chat_session_id"], "header-session")
        self.assertEqual(headers_only["turn_id"], "header-turn")
        self.assertEqual(headers_only["request_id"], "header-req")

        unknown, source_unknown = helpers.resolve_correlation({})
        self.assertEqual(source_unknown, "unknown")
        self.assertEqual(unknown["chat_session_id"], "unknown")
        self.assertEqual(unknown["turn_id"], "unknown")

    def test_sanitize_value_redacts_sensitive_data(self) -> None:
        payload = {
            "api_key": "secret-token",
            "nested": {
                "authorization": "Bearer test",
                "password": "hunter2",
                "safe": "value",
            },
        }
        sanitized = helpers.sanitize_value(payload)
        self.assertEqual(sanitized["api_key"], "<redacted>")
        self.assertEqual(sanitized["nested"]["authorization"], "<redacted>")
        self.assertEqual(sanitized["nested"]["password"], "<redacted>")
        self.assertEqual(sanitized["nested"]["safe"], "value")

    def test_emit_log_event_outputs_schema_compatible_json(self) -> None:
        os.environ["LOG_ENV"] = "test"
        out = io.StringIO()
        with redirect_stdout(out):
            helpers.emit_log_event(
                service="dispatcher",
                event="dispatcher.test",
                message="Dispatcher test event.",
                correlation={
                    "chat_session_id": "session-1",
                    "turn_id": "turn-1",
                    "request_id": "req-1",
                    "tool_call_id": "tool-1",
                },
                correlation_source="payload",
                component="dispatcher",
                operation="test",
                status="ok",
                duration_ms=12,
                attrs={"token": "secret", "safe": "value"},
            )

        raw = out.getvalue().strip()
        self.assertTrue(raw)
        parsed = json.loads(raw)
        self.assertEqual(parsed["service"], "dispatcher")
        self.assertEqual(parsed["event"], "dispatcher.test")
        self.assertEqual(parsed["chat_session_id"], "session-1")
        self.assertEqual(parsed["turn_id"], "turn-1")
        self.assertEqual(parsed["request_id"], "req-1")
        self.assertEqual(parsed["tool_call_id"], "tool-1")
        self.assertEqual(parsed["correlation_source"], "payload")
        self.assertEqual(parsed["attrs"]["token"], "<redacted>")
        self.assertEqual(parsed["attrs"]["safe"], "value")


if __name__ == "__main__":
    unittest.main()
