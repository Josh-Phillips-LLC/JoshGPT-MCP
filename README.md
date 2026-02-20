# JoshGPT-MCP

Containerized MCP server for JoshGPT tool execution.

## Purpose

This repository hosts the MCP tool server that local agents (for example JoshGPT in VS Code) can call for governed local operations.

Current toolset:

Read-only:
- `list_files`
- `read_file`
- `search_text`
- `server_info`

Execution (policy-gated):
- `run_host_command`
- `run_container_command`

## Quick Start

```bash
cp .env.example .env

# Build and run
docker compose up --build
```

Default mode is read-only. To enable execution tools, update `.env`:

```bash
# host execution only
JOSHGPT_MCP_TOOLS_MODE=host

# or host + container execution
JOSHGPT_MCP_TOOLS_MODE=both
JOSHGPT_MCP_ALLOWED_CONTAINERS=implementation-workstation,compliance-workstation,systems-architect-workstation,hr-ai-agent-specialist-workstation
```

## Local Run (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/joshgpt_mcp_server.py
```

## Environment

### Runtime

- `JOSHGPT_MCP_BIND_HOST`: Bind host for MCP runtime (default: `0.0.0.0`)
- `JOSHGPT_MCP_BIND_PORT`: Bind port for MCP runtime (default: `8787`)
- `JOSHGPT_MCP_TRANSPORT`: MCP transport (`stdio`, `sse`, `streamable-http`; default: `streamable-http`)

### Docker Publish

- `JOSHGPT_MCP_PUBLISH_HOST`: Host interface for exposed container port (default: `127.0.0.1`)
- `JOSHGPT_MCP_PUBLISH_PORT`: Host port mapped to MCP bind port (default: `8787`)
- `JOSHGPT_MCP_WORKSPACE_HOST_PATH`: Host path mounted into container as `/workspace` (default: `.`)
- `JOSHGPT_MCP_DOCKER_SOCKET_PATH`: Host socket mapped to `/var/run/docker.sock` for container execution tools (default: `/var/run/docker.sock`)

### Policy + Limits

- `JOSHGPT_MCP_ALLOWED_ROOTS`: Comma-separated allowed root paths inside container (default: `/workspace`)
- `JOSHGPT_MCP_DENY_SEGMENTS`: Denied path segments (default includes `.git`, `node_modules`, `.ssh`, etc.)
- `JOSHGPT_MCP_MAX_FILE_BYTES`: Max bytes allowed for `read_file` (default: `512000`)
- `JOSHGPT_MCP_MAX_LIST_ENTRIES`: Max returned entries for `list_files` (default: `500`)
- `JOSHGPT_MCP_MAX_SEARCH_MATCHES`: Max returned matches for `search_text` (default: `500`)
- `JOSHGPT_MCP_TOOLS_MODE`: `read-only` | `host` | `container` | `both` (default: `read-only`)
- `JOSHGPT_MCP_ALLOWED_HOST_COMMANDS`: Host command allowlist by command basename (comma-separated)
- `JOSHGPT_MCP_ALLOWED_CONTAINER_COMMANDS`: Container command allowlist by command basename
- `JOSHGPT_MCP_ALLOWED_CONTAINERS`: Allowed container names (comma-separated, `*` for all)
- `JOSHGPT_MCP_REQUIRE_CONTAINER_RUNNING`: Require target container to be running before `docker exec`
- `JOSHGPT_MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS`: Default timeout for execution tools
- `JOSHGPT_MCP_MAX_COMMAND_TIMEOUT_SECONDS`: Hard timeout cap for execution tools
- `JOSHGPT_MCP_DEFAULT_COMMAND_OUTPUT_CHARS`: Default max chars for stdout/stderr
- `JOSHGPT_MCP_MAX_COMMAND_OUTPUT_CHARS`: Hard output cap for stdout/stderr
- `JOSHGPT_MCP_MAX_COMMAND_ARGS`: Max args count accepted by execution tools
- `JOSHGPT_MCP_MAX_COMMAND_NAME_CHARS`: Max length for command token
- `JOSHGPT_MCP_MAX_ARG_CHARS`: Max length for each argument

## Tool Behavior

- All tool paths are resolved against `JOSHGPT_MCP_ALLOWED_ROOTS`.
- Requests outside allow-roots are rejected.
- Paths containing denied segments are rejected.
- Search uses `ripgrep` (`rg`) when available.
- Execution tools are blocked unless enabled by `JOSHGPT_MCP_TOOLS_MODE`.
- Execution tools enforce command allowlists, timeout bounds, and output truncation.
- `run_container_command` additionally enforces container allowlist and (by default) running-state checks.

## Smoke Checks

```bash
python3 -m py_compile src/joshgpt_mcp_server.py
```
