# JoshGPT-MCP

Containerized MCP services for JoshGPT tool execution and role-routed orchestration.

## Purpose

This repository now contains three runtime services:

1. `joshgpt-mcp` (tool-host)
   - file/search tools
   - optional command execution tools
2. `joshgpt-dispatcher` (capability)
   - task routing/state for role workers
   - supervisor question/response linkage
3. `joshgpt-supervisor-capability` (capability)
   - context-agnostic supervisor decision engine
   - requires role-context injection from caller

The role mission context remains with workers/roles, not with dispatcher/supervisor capabilities.

## Architecture

- Single repo, separated services/containers.
- Dispatcher and supervisor are **capabilities**, not roles.
- Supervisor capability is contract-driven and context-agnostic.
- Role workers are expected to inject role context in supervisor requests.

## Contracts

Versioned supervisor schemas:

- `contracts/supervisor/v1/request.schema.json`
- `contracts/supervisor/v1/response.schema.json`

Fail-closed rule:
- If role context is missing or ambiguous, supervisor capability returns a deterministic refusal (`status=refused`, `decision=deny`).

## Services and Tools

### 1) Tool-host (`joshgpt-mcp`)

Read-only:
- `list_files`
- `read_file`
- `search_text`
- `server_info`

Execution (policy-gated):
- `run_host_command`
- `run_container_command`

### 2) Dispatcher (`joshgpt-dispatcher`)

- `dispatch_role_task`
- `claim_next_task`
- `set_task_status`
- `submit_supervisor_question`
- `list_pending_supervisor_questions`
- `respond_supervisor_question`
- `get_task_status`
- `list_role_queues`
- `dispatcher_info`

### 3) Supervisor capability (`joshgpt-supervisor-capability`)

- `ask_supervisor_capability`

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

### Supervisor capability
- `JOSHGPT_SUPERVISOR_BIND_HOST`
- `JOSHGPT_SUPERVISOR_BIND_PORT`
- `JOSHGPT_SUPERVISOR_TRANSPORT`
- `JOSHGPT_SUPERVISOR_PUBLISH_HOST`
- `JOSHGPT_SUPERVISOR_PUBLISH_PORT`
- `JOSHGPT_SUPERVISOR_REQUIRE_SHARED_TOKEN`
- `JOSHGPT_SUPERVISOR_SHARED_TOKEN`
- `JOSHGPT_SUPERVISOR_DEFAULT_ESCALATE_ROLE`

## Smoke Checks

```bash
python3 -m py_compile src/joshgpt_mcp_server.py src/dispatcher_mcp_server.py src/supervisor_capability_server.py
```

```bash
docker compose logs --tail=100 joshgpt-dispatcher
docker compose logs --tail=100 joshgpt-supervisor-capability
```

## Notes

- Dispatcher uses SQLite for MVP state.
- Shared token protection is enabled by default for dispatcher and supervisor tools.
- This repo currently defines capability services and contracts; role-worker runtime integration remains a separate implementation phase.
