# JoshGPT-MCP

Containerized MCP server for JoshGPT tool execution.

## Purpose

This repository hosts the MCP tool server that local agents (for example JoshGPT in VS Code) can call for governed, read-only repository operations.

Current toolset:

- `list_files`
- `read_file`
- `search_text`
- `server_info`

## Quick Start

```bash
cp .env.example .env

# Build and run
docker compose up --build
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

### Policy + Limits

- `JOSHGPT_MCP_ALLOWED_ROOTS`: Comma-separated allowed root paths inside container (default: `/workspace`)
- `JOSHGPT_MCP_DENY_SEGMENTS`: Denied path segments (default includes `.git`, `node_modules`, `.ssh`, etc.)
- `JOSHGPT_MCP_MAX_FILE_BYTES`: Max bytes allowed for `read_file` (default: `512000`)
- `JOSHGPT_MCP_MAX_LIST_ENTRIES`: Max returned entries for `list_files` (default: `500`)
- `JOSHGPT_MCP_MAX_SEARCH_MATCHES`: Max returned matches for `search_text` (default: `500`)

## Tool Behavior

- All tool paths are resolved against `JOSHGPT_MCP_ALLOWED_ROOTS`.
- Requests outside allow-roots are rejected.
- Paths containing denied segments are rejected.
- Search uses `ripgrep` (`rg`) when available.

## Next Step Roadmap

- Add optional write/command tools behind explicit policy toggles.
- Add JSON-schema contracts for tool request/response payload governance.
- Add CI smoke test for tool call correctness.
