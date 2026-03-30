# JoshGPT-MCP

Containerized MCP services for JoshGPT tool execution and role-routed orchestration.

## Purpose

This repository contains three runtime services:

1. `toolhost` (tool-host)
   - file/search tools
   - optional command execution tools
2. `dispatcher` (capability)
   - task routing/state for role workers
   - supervisor question/response linkage
3. `supervisor` (capability)
   - canonical Codex supervisor decision engine (`ask_codex_supervisor`)
   - Codex CLI-backed in this phase

## Architecture

- Single repo, separated services/containers.
- Dispatcher and supervisor are capabilities, not roles.
- Supervisor capability uses canonical `ask-codex-supervisor` contracts.
- Role workers inject escalation payloads and consume supervisor decisions.

## Contracts

Canonical supervisor schemas:

- `contracts/ask-codex-supervisor.request.schema.json`
- `contracts/ask-codex-supervisor.response.schema.json`
- `contracts/ask-codex-supervisor.tool.json`

Fail-safe rule:
- If payload validation or Codex runtime execution fails, supervisor returns a deterministic schema-valid `pause_for_human` response.

## Structured Logging

All three services emit one JSON log event per operation to stdout for Loki/Vector ingestion.

- `service=toolhost` from `src/joshgpt_mcp_server.py`
- `service=dispatcher` from `src/dispatcher_mcp_server.py`
- `service=supervisor` from `src/supervisor_capability_server.py`

Correlation resolution order:

1. explicit `correlation` argument
2. `payload.correlation` object
3. inbound headers (`x-chat-session-id`, `x-turn-id`, `x-request-id`, `x-tool-call-id`) when available
4. fallback `unknown`

## Services and Tools

### 1) Tool-host (`toolhost`)

Read-only:
- `list_files`
- `read_file`
- `search_text`
- `server_info`

Execution (policy-gated):
- `run_host_command`
- `run_container_command`

### 2) Dispatcher (`dispatcher`)

- `dispatch_role_task`
- `claim_next_task`
- `set_task_status`
- `submit_supervisor_question`
- `list_pending_supervisor_questions`
- `respond_supervisor_question`
- `list_role_catalog`
- `get_supervisor_role_context`
- `get_task_status`
- `list_role_queues`
- `dispatcher_info`

### 3) Supervisor capability (`supervisor`)

- `ask_codex_supervisor`

Public compatibility note:
- `ask_codex_supervisor` request schema now accepts optional `correlation` object (non-breaking).
- Dispatcher tools accept optional `correlation` argument (non-breaking).

## Quick Start

```bash
cp .env.example .env
# set shared tokens before start

docker compose up --build -d
docker compose ps
```

Default ports:
- Tool-host: `127.0.0.1:8787`
- Dispatcher: `127.0.0.1:8788`
- Supervisor capability: `127.0.0.1:8789`

## Codex CLI Login (Supervisor)

Supervisor requires authenticated `codex` CLI session inside the supervisor runtime.

```bash
docker exec -it jgp-supervisor codex login --device-auth
docker exec jgp-supervisor codex login status
```

## Local Run (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# tool-host
python src/joshgpt_mcp_server.py

# dispatcher (separate terminal)
python src/dispatcher_mcp_server.py

# supervisor capability (separate terminal)
python src/supervisor_capability_server.py
```

## Environment

### Tool-host
- `JOSHGPT_MCP_*` variables in `.env.example`

### Dispatcher
- `JOSHGPT_DISPATCHER_BIND_HOST`
- `JOSHGPT_DISPATCHER_BIND_PORT`
- `JOSHGPT_DISPATCHER_TRANSPORT`
- `JOSHGPT_DISPATCHER_PUBLISH_HOST`
- `JOSHGPT_DISPATCHER_PUBLISH_PORT`
- `JOSHGPT_DISPATCHER_DB_PATH`
- `JOSHGPT_DISPATCHER_REQUIRE_SHARED_TOKEN`
- `JOSHGPT_DISPATCHER_SHARED_TOKEN`
- `JOSHGPT_ROLE_REGISTRY_MOUNT_PATH` (compose host path for canonical registry file)
- `JOSHGPT_ROLE_REPOS_BASE_MOUNT_PATH` (compose host path for role repos root)
- `JOSHGPT_ROLE_REGISTRY_PATH`
- `JOSHGPT_ROLE_REPOS_BASE_PATH`
- `JOSHGPT_SUPERVISOR_CONTEXT_MAX_CHARS`

### Supervisor capability
- `JOSHGPT_SUPERVISOR_BIND_HOST`
- `JOSHGPT_SUPERVISOR_BIND_PORT`
- `JOSHGPT_SUPERVISOR_TRANSPORT`
- `JOSHGPT_SUPERVISOR_PUBLISH_HOST`
- `JOSHGPT_SUPERVISOR_PUBLISH_PORT`
- `JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN`
- `JOSHGPT_SUPERVISOR_SHARED_TOKEN`
- `JOSHGPT_SUPERVISOR_CODEX_CLI_BIN`
- `JOSHGPT_SUPERVISOR_CODEX_CLI_TIMEOUT_SECONDS`
- `JOSHGPT_SUPERVISOR_CODEX_MODEL`
- `JOSHGPT_SUPERVISOR_CODEX_STATE_VOLUME`

## Smoke Checks

```bash
python3 -m py_compile src/joshgpt_mcp_server.py src/dispatcher_mcp_server.py src/supervisor_capability_server.py
```

```bash
docker compose logs --tail=100 dispatcher
docker compose logs --tail=100 supervisor
```

### End-to-End Role/Supervisor Loop (MCP-only)

Run the canonical smoke script from repo root:

```bash
.venv/bin/python scripts/smoke_worker_supervisor_loop.py --summary-only
```

Optional example with explicit tokens/endpoints:

```bash
.venv/bin/python scripts/smoke_worker_supervisor_loop.py \
  --dispatcher-url http://127.0.0.1:8788/mcp \
  --supervisor-url http://127.0.0.1:8789/mcp \
  --dispatcher-shared-token replace-me-dispatcher-token \
  --supervisor-shared-token replace-me-supervisor-token \
  --chat-session-id smoke-session-001 \
  --turn-id smoke-turn-001 \
  --worker-role-slug implementation-specialist \
  --supervisor-role-slug hr-ai-agent-specialist \
  --requested-decision next_step
```

Script sequence:
- dispatch task
- claim task
- submit supervisor question
- fetch selected supervisor `instruction_context` via dispatcher
- call `ask_codex_supervisor`
- record supervisor response
- fetch final task status/events

## Notes

- Dispatcher uses SQLite for MVP state.
- Shared token protection is enabled by default for dispatcher and supervisor tools.
- Supervisor backend is Codex CLI only in this phase.
