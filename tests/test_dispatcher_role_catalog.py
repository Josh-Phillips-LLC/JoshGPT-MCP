#!/usr/bin/env python3
"""Dispatcher role catalog/context tool tests."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

os.environ.setdefault("JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN", "false")

import dispatcher_mcp_server as server  # noqa: E402


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class DispatcherRoleCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.registry_path = self.base / "00-os" / "role-registry.yml"
        self.repos_base = self.base / "repos"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_registry(self, body: str) -> None:
        _write_file(
            self.registry_path,
            textwrap.dedent(body).strip() + "\n",
        )

    def _patch_runtime(self, *, max_chars: int = 12000) -> mock._patch:
        return mock.patch.multiple(
            server,
            JOSHGPT_ROLE_REGISTRY_PATH=self.registry_path,
            JOSHGPT_ROLE_REPOS_BASE_PATH=self.repos_base,
            JOSHGPT_SUPERVISOR_CONTEXT_MAX_CHARS=max_chars,
        )

    def test_list_role_catalog_success_sorted_by_menu_order(self) -> None:
        self._write_registry(
            """
            metadata:
              version: "1.2"
              canonical_source: "00-os/role-registry.yml"
            roles:
              - slug: z-role
                display_name: Zeta Role
                repo_name: repo-z
                menu_order: 20
              - slug: a-role
                display_name: Alpha Role
                repo_name: repo-a
                menu_order: 10
            """
        )

        with self._patch_runtime():
            output = server.list_role_catalog(shared_token="")

        self.assertEqual(output["registry_source"], "00-os/role-registry.yml")
        self.assertEqual(output["registry_version"], "1.2")
        self.assertEqual([role["slug"] for role in output["roles"]], ["a-role", "z-role"])

    def test_list_role_catalog_failure_when_registry_missing(self) -> None:
        with self._patch_runtime():
            with self.assertRaises(FileNotFoundError):
                server.list_role_catalog(shared_token="")

    def test_list_role_catalog_failure_when_registry_invalid(self) -> None:
        self._write_registry("roles: [")

        with self._patch_runtime():
            with self.assertRaises(Exception):
                server.list_role_catalog(shared_token="")

    def test_get_supervisor_role_context_success_with_hash_and_excerpt_bounds(self) -> None:
        self._write_registry(
            """
            metadata:
              version: "2.0"
              canonical_source: "00-os/role-registry.yml"
            roles:
              - slug: hr-ai-agent-specialist
                display_name: HR and AI Agent Specialist
                repo_name: context-engineering-role-hr-ai-agent-specialist
                menu_order: 4
            """
        )

        role_repo = self.repos_base / "context-engineering-role-hr-ai-agent-specialist"
        _write_file(role_repo / "AGENTS.md", "A" * 100)
        _write_file(role_repo / ".github" / "copilot-instructions.md", "B" * 120)

        with self._patch_runtime(max_chars=32):
            output = server.get_supervisor_role_context(
                role_slug="hr-ai-agent-specialist",
                shared_token="",
            )

        instruction = output["instruction_context"]
        self.assertEqual(instruction["role_slug"], "hr-ai-agent-specialist")
        self.assertEqual(instruction["registry_source"], "00-os/role-registry.yml")
        self.assertTrue(instruction["context_ref"])
        self.assertEqual(len(instruction["context_sha256"]), 64)
        self.assertLessEqual(len(instruction["agents_excerpt"]), 32)
        self.assertLessEqual(len(instruction["runtime_policy_excerpt"]), 32)

    def test_get_supervisor_role_context_unknown_slug_failure(self) -> None:
        self._write_registry(
            """
            metadata:
              version: "1.0"
            roles:
              - slug: implementation-specialist
                display_name: Implementation Specialist
                repo_name: context-engineering-role-implementation-specialist
                menu_order: 1
            """
        )

        with self._patch_runtime():
            with self.assertRaises(LookupError):
                server.get_supervisor_role_context(role_slug="does-not-exist", shared_token="")

    def test_get_supervisor_role_context_missing_agents_failure(self) -> None:
        self._write_registry(
            """
            metadata:
              version: "1.0"
              canonical_source: "00-os/role-registry.yml"
            roles:
              - slug: implementation-specialist
                display_name: Implementation Specialist
                repo_name: context-engineering-role-implementation-specialist
                menu_order: 1
            """
        )

        role_repo = self.repos_base / "context-engineering-role-implementation-specialist"
        _write_file(
            role_repo / ".github" / "copilot-instructions.md",
            "runtime policy",
        )

        with self._patch_runtime():
            with self.assertRaises(FileNotFoundError):
                server.get_supervisor_role_context(
                    role_slug="implementation-specialist",
                    shared_token="",
                )


if __name__ == "__main__":
    unittest.main()
