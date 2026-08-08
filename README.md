# Potato Gateway

Potato Gateway is the unified HTTP API entrance for Potato Hub, ChatGPT GPT Actions, Hermes Agent, and future Codex workflows. It is an independent FastAPI service and does not replace or modify the existing Potato Hub process.

## Current Capabilities

- `GET /health`: public process health check.
- `GET /api/status`: authenticated Gateway, Potato Hub, and real Runner heartbeat status.
- `GET /api/agents/{agent_id}/profile`: authenticated, read-only Hermes Profile and Prompt fingerprint for four Agents.
- `POST /api/calibrations`: create an idempotent `manual` or `hub` calibration session for the three calibratable Agents.
- `GET /api/calibrations/{session_id}`: read a Session and all recorded Turns.
- `POST /api/calibrations/{session_id}/turns`: append an idempotent manual Turn.
- `GET /api/agents/{agent_id}/calibrations`: list an Agent's Sessions newest first.
- Asynchronous calibration execution: queue a real Agent turn, then poll by execution ID.
- Historical delivery calibration: select an existing Hub video plus storyboard, timeline, script, manifest, subtitle, audio, cover, or other supporting Assets without calling the creator.
- Versioned calibration Submissions: one primary video per version, optional parent version, safe previews, and manual critic review.
- Video workflow Actions: create, inspect, message, approve, summarize Assets, and read Reviews.
- Immutable Prompt candidates plus an authenticated admin-only publish/rollback path.
- Bearer Token authentication, strict Pydantic responses, path containment checks, and safe request logging.

The profile endpoint only returns an explicit whitelist: Profile name, load status, model provider/name, Skill names, whether Memory is enabled, Prompt version information, and structured calibration state. It does not return Prompt text, raw Hermes configuration, credentials, environment variables, or local paths.

## Agent Calibration

In this project, calibration means iteratively testing and adjusting Prompt, Skills, workflow, tool use, and evaluation criteria. It is not model training or fine-tuning.

A calibration Session stores a goal, acceptance criteria, a snapshot of the current Prompt hash, and an ordered set of Turns. Session state in this phase is limited to `calibrating`, `blocked`, or `closed`.

`transport: manual` only records caller-supplied content. `transport: hub` uses the asynchronous Hermes Runner: `executeCalibrationTurn` immediately returns an execution ID, and `getCalibrationTurn` later synchronizes the real response and Asset IDs from Hub. A completed Hub response is also appended to the isolated calibration Session.

The calibration console at `http://127.0.0.1:8765/calibrations` supports two entry modes. `Historical delivery` creates a Submission directly from Assets already registered in Hub and never calls the creator. `Live generation` keeps the asynchronous Runner path. A historical Submission recommends one available video, infers common supporting-file roles from filenames, and lets the user correct the selection before manually starting critic review.

Fixed test suites live in `config/calibration-suites.yaml`: creator has 3 baseline cases; researcher and critic have 5 each. The global gate is zero hard errors and at least 80/100, but only the user can accept a candidate Prompt. ChatGPT may record a structured critique and create a candidate; it cannot publish one through GPT Actions.

`getAgentProfile` now prioritizes active SQLite Sessions. The latest open Session maps to `calibrating` or `blocked`; if only closed Sessions exist, Profile status is `untracked` because formal evaluation and Prompt promotion are not implemented yet. When SQLite contains no Session for an Agent, the previous `runtime/calibration/{agent_id}.json` format remains available as a compatibility fallback.

`untracked` means the Gateway has no active structured calibration result. It does not imply that the Agent is offline, untested, or unstable, and it never means `stable`.

## Agent Registry

The server-side registry is [config/agents.yaml](config/agents.yaml). HTTP Agent IDs map to real Hermes Profiles there; clients cannot submit Profile paths, Prompt filenames, or arbitrary filesystem paths.

Each registration contains:

- `display_name` and `role` for the public Agent identity.
- `profile_name` for the safe public Hermes Profile name.
- `hermes_profile`, a relative path contained by `POTATO_HERMES_HOME`.
- `prompt_files`, the only Prompt sources the Gateway may read and hash.
- Optional `prompt_metadata_file`, the only explicit version metadata source the Gateway may read.

Without verified metadata, Prompt version is a stable SHA-256 content hash after UTF-8, newline, and Unicode normalization. `version` is returned as `sha256:<first-12-hex>`. If metadata is declared, it must use schema version 1, list the same source files, and contain the full matching SHA-256 hash before its version is accepted.

## Environment

This project uses Python 3.11+ and `uv`:

```bash
cd /Users/zhanghong/.hermes/potato-gateway
uv sync
```

Generate a strong local token:

```bash
export POTATO_GATEWAY_TOKEN="$(openssl rand -hex 32)"
```

Supported settings:

```bash
export POTATO_GATEWAY_HOST=127.0.0.1
export POTATO_GATEWAY_PORT=8765
export POTATO_GATEWAY_LOG_LEVEL=INFO
export POTATO_HERMES_HOME=/Users/zhanghong/.hermes
export POTATO_AGENT_REGISTRY_PATH=/Users/zhanghong/.hermes/potato-gateway/config/agents.yaml
export POTATO_CALIBRATION_STATE_DIR=/Users/zhanghong/.hermes/potato-gateway/runtime/calibration
export POTATO_GATEWAY_DB_PATH=/Users/zhanghong/.hermes/potato-gateway/runtime/potato-gateway.db
export POTATO_HUB_URL=http://127.0.0.1:8787
# POTATO_HUB_TOKEN may be omitted locally; Gateway reads potato-relay/.hub-token.
```

The SQLite database defaults to `runtime/potato-gateway.db`. It uses WAL, foreign keys, a busy timeout, schema migrations, and transactional idempotency constraints. Database, WAL, and shared-memory files are ignored by Git.

Use `.env.example` only as a template. Never commit a real `.env`, token, database, or calibration record containing sensitive material.

## Start

```bash
cd /Users/zhanghong/.hermes/potato-gateway
uv run potato-gateway
```

The default listener is `127.0.0.1:8765`. Do not change it to `0.0.0.0` without reviewing the exposure path, authentication, logs, and documentation endpoints.

Swagger/OpenAPI docs remain enabled for local development. Because this installation is exposed through Tailscale Funnel, reassess whether `/docs`, `/redoc`, and `/openapi.json` should be disabled or protected before broadening access. Funnel or reverse-proxy configuration is outside this change.

## Query Locally

```bash
curl -sS http://127.0.0.1:8765/health
```

```bash
curl -sS http://127.0.0.1:8765/api/status \
  -H "Authorization: Bearer ${POTATO_GATEWAY_TOKEN}"
```

```bash
curl -sS http://127.0.0.1:8765/api/agents/critic/profile \
  -H "Authorization: Bearer ${POTATO_GATEWAY_TOKEN}" \
  | python3 -m json.tool
```

Create a Hub calibration Session:

```bash
curl -sS -X POST http://127.0.0.1:8765/api/calibrations \
  -H "Authorization: Bearer ${POTATO_GATEWAY_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "client_request_id": "cal-create-researcher-001",
    "agent_id": "researcher",
    "transport": "hub",
    "goal": "测试薯博士对近期热点和正宗A股标的的调研能力",
    "acceptance_criteria": ["引用信息来源", "说明股票与产业链的实际关系"]
  }'
```

Save the returned `session_id`, then record a Turn:

```bash
curl -sS -X POST \
  "http://127.0.0.1:8765/api/calibrations/${SESSION_ID}/turns" \
  -H "Authorization: Bearer ${POTATO_GATEWAY_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "client_turn_id": "turn-researcher-001",
    "actor": "commander",
    "kind": "instruction",
    "content": "调研最近两周商业航天的重要事件。"
  }'
```

Query the Session and Agent history:

```bash
curl -sS "http://127.0.0.1:8765/api/calibrations/${SESSION_ID}" \
  -H "Authorization: Bearer ${POTATO_GATEWAY_TOKEN}"

curl -sS \
  "http://127.0.0.1:8765/api/agents/researcher/calibrations?limit=20" \
  -H "Authorization: Bearer ${POTATO_GATEWAY_TOKEN}"
```

`client_request_id` and `client_turn_id` are idempotency keys. Retrying the same ID returns the original object instead of inserting another row.

## Query Through Funnel

```bash
curl -sS \
  https://zhanghongmac-mini.tail282e0b.ts.net/api/agents/critic/profile \
  -H "Authorization: Bearer ${POTATO_GATEWAY_TOKEN}" \
  | python3 -m json.tool
```

Use the same calibration paths and JSON bodies with the Funnel base URL to access these endpoints over HTTPS.

The Funnel terminates HTTPS while Gateway continues to listen only on localhost. This repository does not create or modify Funnel configuration.

## Test

```bash
cd /Users/zhanghong/.hermes/potato-gateway
uv run pytest
```

Tests use temporary Hermes directories and do not modify the three real Profiles.

## Safety Boundaries

- GPT Actions never return local paths, credentials, complete Prompt content, or unapproved logs.
- Prompt candidates are inert until the authenticated admin publish endpoint receives the exact candidate hash; the previous active version becomes a rollback target.
- `engineer` is visible in status/profile and workflow APIs but is excluded from chat calibration.
- Hub is the workflow source of truth. Gateway retries use idempotency keys and do not duplicate Agent execution or paid media generation.
- Manual chats, file timestamps, Git history, or Agent self-reports never imply a passed calibration or a published Prompt.
