#!/usr/bin/env python3
"""JoshGPT MCP server.

Read-only MCP tools for local-agent workflows:
- list_files
- read_file
- search_text

All tool paths are constrained to configured allow-roots.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8787
DEFAULT_TRANSPORT = "streamable-http"
ALLOWED_TRANSPORTS = {"stdio", "sse", "streamable-http"}

DEFAULT_ALLOWED_ROOTS = "/workspace"
DEFAULT_DENY_SEGMENTS = ".git,node_modules,__pycache__,.venv,.ssh,.codex"

DEFAULT_MAX_FILE_BYTES = 512_000
DEFAULT_MAX_LIST_ENTRIES = 500
DEFAULT_MAX_SEARCH_MATCHES = 500


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


def _resolve_transport(raw_transport: str) -> str:
    normalized = raw_transport.strip().lower()
    if normalized in ALLOWED_TRANSPORTS:
        return normalized
    print(
        f"Invalid JOSHGPT_MCP_TRANSPORT={raw_transport!r}; defaulting to {DEFAULT_TRANSPORT}.",
        file=sys.stderr,
    )
    return DEFAULT_TRANSPORT


def _parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_allowed_roots(raw_roots: str) -> list[Path]:
    roots: list[Path] = []
    for value in _parse_csv(raw_roots):
        roots.append(Path(value).expanduser().resolve(strict=False))
    if not roots:
        roots.append(Path(DEFAULT_ALLOWED_ROOTS).resolve(strict=False))
    if not any(root.exists() for root in roots):
        roots.append(Path.cwd().resolve())
    return roots


def _parse_deny_segments(raw_segments: str) -> set[str]:
    return set(_parse_csv(raw_segments))


JOSHGPT_MCP_BIND_HOST = os.getenv("JOSHGPT_MCP_BIND_HOST", DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
JOSHGPT_MCP_BIND_PORT = _env_int("JOSHGPT_MCP_BIND_PORT", DEFAULT_BIND_PORT)
JOSHGPT_MCP_TRANSPORT = _resolve_transport(os.getenv("JOSHGPT_MCP_TRANSPORT", DEFAULT_TRANSPORT))

JOSHGPT_MCP_ALLOWED_ROOTS = _parse_allowed_roots(
    os.getenv("JOSHGPT_MCP_ALLOWED_ROOTS", DEFAULT_ALLOWED_ROOTS)
)
JOSHGPT_MCP_DENY_SEGMENTS = _parse_deny_segments(
    os.getenv("JOSHGPT_MCP_DENY_SEGMENTS", DEFAULT_DENY_SEGMENTS)
)
JOSHGPT_MCP_MAX_FILE_BYTES = _env_int("JOSHGPT_MCP_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)
JOSHGPT_MCP_MAX_LIST_ENTRIES = _env_int("JOSHGPT_MCP_MAX_LIST_ENTRIES", DEFAULT_MAX_LIST_ENTRIES)
JOSHGPT_MCP_MAX_SEARCH_MATCHES = _env_int("JOSHGPT_MCP_MAX_SEARCH_MATCHES", DEFAULT_MAX_SEARCH_MATCHES)

mcp = FastMCP(
    "joshgpt-mcp",
    host=JOSHGPT_MCP_BIND_HOST,
    port=JOSHGPT_MCP_BIND_PORT,
)


def _anchor_root_for_relative_paths() -> Path:
    for root in JOSHGPT_MCP_ALLOWED_ROOTS:
        if root.exists():
            return root
    return Path.cwd().resolve()


def _is_under_allowed_root(path: Path) -> bool:
    for root in JOSHGPT_MCP_ALLOWED_ROOTS:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _matching_root(path: Path) -> Path | None:
    for root in JOSHGPT_MCP_ALLOWED_ROOTS:
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _contains_denied_segment(path: Path) -> bool:
    for segment in path.parts:
        if segment in JOSHGPT_MCP_DENY_SEGMENTS:
            return True
    return False


def _resolve_path(raw_path: str, *, expect: str | None = None, must_exist: bool = True) -> Path:
    path_input = (raw_path or ".").strip()
    candidate = Path(path_input).expanduser()

    if not candidate.is_absolute():
        candidate = _anchor_root_for_relative_paths() / candidate

    candidate = candidate.resolve(strict=False)

    if not _is_under_allowed_root(candidate):
        raise ValueError(
            f"Path outside allowed roots: {candidate}. Allowed roots: "
            f"{', '.join(str(root) for root in JOSHGPT_MCP_ALLOWED_ROOTS)}"
        )

    if _contains_denied_segment(candidate):
        raise ValueError(f"Access denied for path segment policy: {candidate}")

    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"Path does not exist: {candidate}")

    if expect == "file" and not candidate.is_file():
        raise ValueError(f"Expected file path: {candidate}")

    if expect == "dir" and not candidate.is_dir():
        raise ValueError(f"Expected directory path: {candidate}")

    return candidate


def _bounded_limit(requested: int | None, default_limit: int) -> int:
    if requested is None:
        return default_limit
    if requested <= 0:
        return default_limit
    return min(requested, default_limit)


def _safe_size(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


@mcp.tool()
def list_files(path: str = ".", recursive: bool = False, max_entries: int | None = None) -> dict[str, Any]:
    """List files/directories under an allowed root path."""

    target_dir = _resolve_path(path, expect="dir")
    limit = _bounded_limit(max_entries, JOSHGPT_MCP_MAX_LIST_ENTRIES)

    entries: list[dict[str, Any]] = []
    iterator = target_dir.rglob("*") if recursive else target_dir.iterdir()

    for item in iterator:
        candidate = item.resolve(strict=False)

        if not _is_under_allowed_root(candidate):
            continue
        if _contains_denied_segment(candidate):
            continue

        root = _matching_root(candidate)
        relative_path = str(candidate.relative_to(root)) if root else str(candidate)

        entries.append(
            {
                "path": str(candidate),
                "relative_path": relative_path,
                "type": "directory" if item.is_dir() else "file",
                "size_bytes": _safe_size(item),
            }
        )

        if len(entries) >= limit:
            break

    return {
        "path": str(target_dir),
        "recursive": recursive,
        "max_entries": limit,
        "returned": len(entries),
        "entries": entries,
    }


@mcp.tool()
def read_file(path: str, start_line: int = 1, end_line: int = 0) -> dict[str, Any]:
    """Read UTF-8 text from an allowed file path with optional line range."""

    target_file = _resolve_path(path, expect="file")
    size_bytes = target_file.stat().st_size

    if size_bytes > JOSHGPT_MCP_MAX_FILE_BYTES:
        raise ValueError(
            f"File too large ({size_bytes} bytes). "
            f"Current limit: {JOSHGPT_MCP_MAX_FILE_BYTES} bytes."
        )

    text = target_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)

    if total_lines == 0:
        return {
            "path": str(target_file),
            "size_bytes": size_bytes,
            "total_lines": 0,
            "start_line": 1,
            "end_line": 0,
            "content": "",
        }

    start = max(start_line, 1)
    if start > total_lines:
        start = total_lines

    end = total_lines if end_line <= 0 else min(end_line, total_lines)
    if end < start:
        end = start

    excerpt = "\n".join(lines[start - 1 : end])

    return {
        "path": str(target_file),
        "size_bytes": size_bytes,
        "total_lines": total_lines,
        "start_line": start,
        "end_line": end,
        "content": excerpt,
    }


def _parse_search_output_line(line: str) -> tuple[str, int, str] | None:
    # Expected pattern: <path>:<line>:<content>
    match = re.match(r"^(.*?):([0-9]+):(.*)$", line)
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)


@mcp.tool()
def search_text(
    pattern: str,
    path: str = ".",
    glob: str = "",
    max_matches: int | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Search text under an allowed path using ripgrep with safe fallbacks."""

    if not pattern.strip():
        raise ValueError("pattern must not be empty")

    target = _resolve_path(path)
    limit = _bounded_limit(max_matches, JOSHGPT_MCP_MAX_SEARCH_MATCHES)

    rg_bin = shutil.which("rg")
    if rg_bin:
        cmd = [
            rg_bin,
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(limit),
        ]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if glob.strip():
            cmd.extend(["--glob", glob.strip()])
        cmd.extend([pattern, str(target)])
    else:
        cmd = ["grep", "-RIn", pattern, str(target)]
        if not case_sensitive:
            cmd.insert(1, "-i")

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode not in (0, 1):
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"search command failed: {detail[:400]}")

    matches: list[dict[str, Any]] = []
    for raw_line in completed.stdout.splitlines():
        parsed = _parse_search_output_line(raw_line)
        if parsed is None:
            continue

        file_part, line_number, content = parsed
        candidate_path = Path(file_part).resolve(strict=False)

        if not _is_under_allowed_root(candidate_path):
            continue
        if _contains_denied_segment(candidate_path):
            continue

        matches.append(
            {
                "path": str(candidate_path),
                "line": line_number,
                "text": content,
            }
        )

        if len(matches) >= limit:
            break

    return {
        "pattern": pattern,
        "path": str(target),
        "glob": glob,
        "case_sensitive": case_sensitive,
        "max_matches": limit,
        "returned": len(matches),
        "matches": matches,
    }


@mcp.tool()
def server_info() -> dict[str, Any]:
    """Expose active server limits and policy settings."""

    return {
        "name": "joshgpt-mcp",
        "transport": JOSHGPT_MCP_TRANSPORT,
        "bind_host": JOSHGPT_MCP_BIND_HOST,
        "bind_port": JOSHGPT_MCP_BIND_PORT,
        "allowed_roots": [str(root) for root in JOSHGPT_MCP_ALLOWED_ROOTS],
        "deny_segments": sorted(JOSHGPT_MCP_DENY_SEGMENTS),
        "max_file_bytes": JOSHGPT_MCP_MAX_FILE_BYTES,
        "max_list_entries": JOSHGPT_MCP_MAX_LIST_ENTRIES,
        "max_search_matches": JOSHGPT_MCP_MAX_SEARCH_MATCHES,
    }


if __name__ == "__main__":
    print(
        (
            "Starting joshgpt-mcp with "
            f"transport={JOSHGPT_MCP_TRANSPORT} "
            f"host={JOSHGPT_MCP_BIND_HOST} "
            f"port={JOSHGPT_MCP_BIND_PORT} "
            f"allowed_roots={[str(root) for root in JOSHGPT_MCP_ALLOWED_ROOTS]}"
        ),
        flush=True,
    )
    mcp.run(transport=JOSHGPT_MCP_TRANSPORT)
