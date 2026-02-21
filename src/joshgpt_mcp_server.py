#!/usr/bin/env python3
"""JoshGPT MCP server.

Tool groups:
- Read-only file tools:
  - list_files
  - read_file
  - search_text
- Optional execution tools (env-policy controlled):
  - run_host_command
  - run_container_command

Execution tools are disabled by default via JOSHGPT_MCP_TOOLS_MODE=read-only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
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

DEFAULT_TOOLS_MODE = "read-only"
ALLOWED_TOOLS_MODES = {"read-only", "host", "container", "both"}

DEFAULT_ALLOWED_HOST_COMMANDS = (
    "ls,cat,rg,grep,find,pwd,echo,git,gh,python,python3,"
    "sed,awk,head,tail,wc,stat,uname,date,id,whoami"
)
DEFAULT_ALLOWED_CONTAINER_COMMANDS = (
    "ls,cat,rg,grep,find,pwd,echo,git,gh,python,python3,"
    "sed,awk,head,tail,wc,stat,uname,date,id,whoami"
)
DEFAULT_ALLOWED_CONTAINERS = ""
DEFAULT_REQUIRE_CONTAINER_RUNNING = True

DEFAULT_DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
DEFAULT_MAX_COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_DEFAULT_COMMAND_OUTPUT_CHARS = 12_000
DEFAULT_MAX_COMMAND_OUTPUT_CHARS = 40_000
DEFAULT_MAX_COMMAND_ARGS = 32
DEFAULT_MAX_COMMAND_NAME_CHARS = 128
DEFAULT_MAX_ARG_CHARS = 1024


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


def _parse_tools_mode(raw_mode: str) -> str:
    normalized = (raw_mode or "").strip().lower()
    if normalized in ALLOWED_TOOLS_MODES:
        return normalized
    print(
        f"Invalid JOSHGPT_MCP_TOOLS_MODE={raw_mode!r}; defaulting to {DEFAULT_TOOLS_MODE}.",
        file=sys.stderr,
    )
    return DEFAULT_TOOLS_MODE


def _parse_allowlist(raw: str) -> set[str]:
    values = _parse_csv(raw)
    return set(values)


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

JOSHGPT_MCP_TOOLS_MODE = _parse_tools_mode(os.getenv("JOSHGPT_MCP_TOOLS_MODE", DEFAULT_TOOLS_MODE))
JOSHGPT_MCP_ALLOWED_HOST_COMMANDS = _parse_allowlist(
    os.getenv("JOSHGPT_MCP_ALLOWED_HOST_COMMANDS", DEFAULT_ALLOWED_HOST_COMMANDS)
)
JOSHGPT_MCP_ALLOWED_CONTAINER_COMMANDS = _parse_allowlist(
    os.getenv("JOSHGPT_MCP_ALLOWED_CONTAINER_COMMANDS", DEFAULT_ALLOWED_CONTAINER_COMMANDS)
)
JOSHGPT_MCP_ALLOWED_CONTAINERS = _parse_allowlist(
    os.getenv("JOSHGPT_MCP_ALLOWED_CONTAINERS", DEFAULT_ALLOWED_CONTAINERS)
)
JOSHGPT_MCP_REQUIRE_CONTAINER_RUNNING = _env_bool(
    "JOSHGPT_MCP_REQUIRE_CONTAINER_RUNNING", DEFAULT_REQUIRE_CONTAINER_RUNNING
)

JOSHGPT_MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS = _env_int(
    "JOSHGPT_MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS",
    DEFAULT_DEFAULT_COMMAND_TIMEOUT_SECONDS,
)
JOSHGPT_MCP_MAX_COMMAND_TIMEOUT_SECONDS = _env_int(
    "JOSHGPT_MCP_MAX_COMMAND_TIMEOUT_SECONDS",
    DEFAULT_MAX_COMMAND_TIMEOUT_SECONDS,
)
JOSHGPT_MCP_DEFAULT_COMMAND_OUTPUT_CHARS = _env_int(
    "JOSHGPT_MCP_DEFAULT_COMMAND_OUTPUT_CHARS",
    DEFAULT_DEFAULT_COMMAND_OUTPUT_CHARS,
)
JOSHGPT_MCP_MAX_COMMAND_OUTPUT_CHARS = _env_int(
    "JOSHGPT_MCP_MAX_COMMAND_OUTPUT_CHARS",
    DEFAULT_MAX_COMMAND_OUTPUT_CHARS,
)
JOSHGPT_MCP_MAX_COMMAND_ARGS = _env_int(
    "JOSHGPT_MCP_MAX_COMMAND_ARGS",
    DEFAULT_MAX_COMMAND_ARGS,
)
JOSHGPT_MCP_MAX_COMMAND_NAME_CHARS = _env_int(
    "JOSHGPT_MCP_MAX_COMMAND_NAME_CHARS",
    DEFAULT_MAX_COMMAND_NAME_CHARS,
)
JOSHGPT_MCP_MAX_ARG_CHARS = _env_int("JOSHGPT_MCP_MAX_ARG_CHARS", DEFAULT_MAX_ARG_CHARS)

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


def _mode_allows(tool_area: str) -> bool:
    if JOSHGPT_MCP_TOOLS_MODE == "both":
        return True
    if JOSHGPT_MCP_TOOLS_MODE == "read-only":
        return False
    return JOSHGPT_MCP_TOOLS_MODE == tool_area


def _assert_mode_allows(tool_area: str) -> None:
    if _mode_allows(tool_area):
        return
    raise PermissionError(
        f"Tool is disabled by JOSHGPT_MCP_TOOLS_MODE={JOSHGPT_MCP_TOOLS_MODE!r}. "
        f"Required mode: {tool_area!r} or 'both'."
    )


def _has_forbidden_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 for ch in value)


def _validate_command(command: str, allowlist: set[str], label: str) -> tuple[str, str]:
    token = (command or "").strip()
    if not token:
        raise ValueError("command must not be empty")
    if " " in token:
        raise ValueError("command must be a single token (pass arguments via args)")
    if "/" in token or "\\" in token:
        raise ValueError("command must be a basename token (path separators are not allowed)")
    if len(token) > JOSHGPT_MCP_MAX_COMMAND_NAME_CHARS:
        raise ValueError(
            f"command exceeds max length ({JOSHGPT_MCP_MAX_COMMAND_NAME_CHARS} chars)"
        )
    if _has_forbidden_control_chars(token):
        raise ValueError("command contains forbidden control characters")

    basename = Path(token).name
    if basename in {"", ".", ".."}:
        raise ValueError("command is invalid")

    if "*" not in allowlist and basename not in allowlist:
        allowed = ", ".join(sorted(allowlist)) if allowlist else "<empty>"
        raise PermissionError(
            f"{label} command not allowed: {basename!r}. "
            f"Set {label} allowlist env var to permit it. Current allowlist: {allowed}"
        )

    return token, basename


def _validate_args(args: list[str] | None) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list):
        raise ValueError("args must be an array of strings")
    if len(args) > JOSHGPT_MCP_MAX_COMMAND_ARGS:
        raise ValueError(f"args exceeds max count ({JOSHGPT_MCP_MAX_COMMAND_ARGS})")

    cleaned: list[str] = []
    for raw in args:
        item = str(raw)
        if _has_forbidden_control_chars(item):
            raise ValueError("args contain forbidden control characters")
        if len(item) > JOSHGPT_MCP_MAX_ARG_CHARS:
            raise ValueError(
                f"argument exceeds max length ({JOSHGPT_MCP_MAX_ARG_CHARS} chars)"
            )
        cleaned.append(item)
    return cleaned


def _bounded_timeout_seconds(requested: int | None) -> int:
    if requested is None or requested <= 0:
        return JOSHGPT_MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS
    return min(requested, JOSHGPT_MCP_MAX_COMMAND_TIMEOUT_SECONDS)


def _bounded_output_chars(requested: int | None) -> int:
    if requested is None or requested <= 0:
        return JOSHGPT_MCP_DEFAULT_COMMAND_OUTPUT_CHARS
    return min(requested, JOSHGPT_MCP_MAX_COMMAND_OUTPUT_CHARS)


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    text = value or ""
    if len(text) <= limit:
        return text, False
    suffix = "\n...[truncated]"
    head_budget = max(0, limit - len(suffix))
    return f"{text[:head_budget]}{suffix}", True


def _coerce_timeout_partial(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_command(
    argv: list[str],
    *,
    cwd: Path | None,
    timeout_seconds: int,
    max_output_chars: int,
) -> dict[str, Any]:
    started_at = time.time()
    timed_out = False

    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        exit_code = completed.returncode
        stdout_text = completed.stdout or ""
        stderr_text = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout_text = _coerce_timeout_partial(exc.stdout)
        stderr_text = _coerce_timeout_partial(exc.stderr)

    stdout_text, stdout_truncated = _truncate_text(stdout_text, max_output_chars)
    stderr_text, stderr_truncated = _truncate_text(stderr_text, max_output_chars)

    duration_ms = int((time.time() - started_at) * 1000)

    return {
        "argv": argv,
        "cwd": str(cwd) if cwd else None,
        "timeout_seconds": timeout_seconds,
        "max_output_chars": max_output_chars,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def _validate_container_name(container_name: str) -> str:
    name = (container_name or "").strip()
    if not name:
        raise ValueError("container_name must not be empty")
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", name):
        raise ValueError("container_name contains invalid characters")
    if _has_forbidden_control_chars(name):
        raise ValueError("container_name contains forbidden control characters")

    if not JOSHGPT_MCP_ALLOWED_CONTAINERS:
        raise PermissionError(
            "JOSHGPT_MCP_ALLOWED_CONTAINERS is empty. "
            "Add allowed container names (comma-separated) or '*' to permit all."
        )

    if "*" not in JOSHGPT_MCP_ALLOWED_CONTAINERS and name not in JOSHGPT_MCP_ALLOWED_CONTAINERS:
        allowed = ", ".join(sorted(JOSHGPT_MCP_ALLOWED_CONTAINERS))
        raise PermissionError(
            f"Container not allowed: {name!r}. Allowed containers: {allowed}"
        )

    return name


def _validate_container_workdir(raw_workdir: str) -> str | None:
    workdir = (raw_workdir or "").strip()
    if not workdir:
        return None
    if _has_forbidden_control_chars(workdir):
        raise ValueError("container workdir contains forbidden control characters")
    if not workdir.startswith("/"):
        raise ValueError("container workdir must be an absolute path")
    return workdir


def _docker_bin() -> str:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        raise RuntimeError(
            "docker CLI is unavailable. Install docker CLI and ensure it is on PATH."
        )
    return docker_bin


def _ensure_container_running(docker_bin: str, container_name: str) -> None:
    completed = subprocess.run(
        [docker_bin, "inspect", "-f", "{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Unable to inspect container {container_name!r}: {detail[:300]}")

    state = (completed.stdout or "").strip().lower()
    if state != "true":
        raise RuntimeError(f"Container is not running: {container_name!r}")


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
def run_host_command(
    command: str,
    args: list[str] | None = None,
    cwd: str = ".",
    timeout_seconds: int | None = None,
    max_output_chars: int | None = None,
) -> dict[str, Any]:
    """Run an allowlisted command on the MCP host with bounded timeout/output."""

    _assert_mode_allows("host")

    command_token, basename = _validate_command(
        command,
        JOSHGPT_MCP_ALLOWED_HOST_COMMANDS,
        "host",
    )
    cleaned_args = _validate_args(args)
    resolved_cwd = _resolve_path(cwd, expect="dir")
    executable = shutil.which(command_token)
    if not executable:
        raise FileNotFoundError(f"Host command not found on PATH: {command_token!r}")

    timeout = _bounded_timeout_seconds(timeout_seconds)
    output_limit = _bounded_output_chars(max_output_chars)

    result = _run_command(
        [executable, *cleaned_args],
        cwd=resolved_cwd,
        timeout_seconds=timeout,
        max_output_chars=output_limit,
    )

    return {
        "tool": "run_host_command",
        "mode": JOSHGPT_MCP_TOOLS_MODE,
        "command": basename,
        "executable": executable,
        "args": cleaned_args,
        "cwd": str(resolved_cwd),
        "result": result,
    }


@mcp.tool()
def run_container_command(
    container_name: str,
    command: str,
    args: list[str] | None = None,
    workdir: str = "",
    timeout_seconds: int | None = None,
    max_output_chars: int | None = None,
) -> dict[str, Any]:
    """Run an allowlisted command inside an allowlisted container using docker exec."""

    _assert_mode_allows("container")

    validated_container = _validate_container_name(container_name)
    command_token, basename = _validate_command(
        command,
        JOSHGPT_MCP_ALLOWED_CONTAINER_COMMANDS,
        "container",
    )
    cleaned_args = _validate_args(args)
    validated_workdir = _validate_container_workdir(workdir)

    docker_bin = _docker_bin()
    if JOSHGPT_MCP_REQUIRE_CONTAINER_RUNNING:
        _ensure_container_running(docker_bin, validated_container)

    timeout = _bounded_timeout_seconds(timeout_seconds)
    output_limit = _bounded_output_chars(max_output_chars)

    docker_cmd = [docker_bin, "exec"]
    if validated_workdir:
        docker_cmd.extend(["-w", validated_workdir])
    docker_cmd.append(validated_container)
    docker_cmd.append(command_token)
    docker_cmd.extend(cleaned_args)

    result = _run_command(
        docker_cmd,
        cwd=None,
        timeout_seconds=timeout,
        max_output_chars=output_limit,
    )

    return {
        "tool": "run_container_command",
        "mode": JOSHGPT_MCP_TOOLS_MODE,
        "container_name": validated_container,
        "command": basename,
        "args": cleaned_args,
        "workdir": validated_workdir,
        "result": result,
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
        "tools_mode": JOSHGPT_MCP_TOOLS_MODE,
        "allowed_host_commands": sorted(JOSHGPT_MCP_ALLOWED_HOST_COMMANDS),
        "allowed_container_commands": sorted(JOSHGPT_MCP_ALLOWED_CONTAINER_COMMANDS),
        "allowed_containers": sorted(JOSHGPT_MCP_ALLOWED_CONTAINERS),
        "require_container_running": JOSHGPT_MCP_REQUIRE_CONTAINER_RUNNING,
        "default_command_timeout_seconds": JOSHGPT_MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS,
        "max_command_timeout_seconds": JOSHGPT_MCP_MAX_COMMAND_TIMEOUT_SECONDS,
        "default_command_output_chars": JOSHGPT_MCP_DEFAULT_COMMAND_OUTPUT_CHARS,
        "max_command_output_chars": JOSHGPT_MCP_MAX_COMMAND_OUTPUT_CHARS,
        "max_command_args": JOSHGPT_MCP_MAX_COMMAND_ARGS,
        "max_command_name_chars": JOSHGPT_MCP_MAX_COMMAND_NAME_CHARS,
        "max_arg_chars": JOSHGPT_MCP_MAX_ARG_CHARS,
    }


if __name__ == "__main__":
    print(
        (
            "Starting joshgpt-mcp with "
            f"transport={JOSHGPT_MCP_TRANSPORT} "
            f"host={JOSHGPT_MCP_BIND_HOST} "
            f"port={JOSHGPT_MCP_BIND_PORT} "
            f"mode={JOSHGPT_MCP_TOOLS_MODE} "
            f"allowed_roots={[str(root) for root in JOSHGPT_MCP_ALLOWED_ROOTS]}"
        ),
        flush=True,
    )
    mcp.run(transport=JOSHGPT_MCP_TRANSPORT)
