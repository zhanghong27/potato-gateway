# Potato Gateway

Potato Gateway is the first-phase unified HTTP API entrance for Potato Hub, future ChatGPT GPT Actions, Hermes Agent, and Codex workflows.

This service is intentionally small. It does not replace the existing Potato Hub server and does not call its real task or message APIs yet.

## Current Capabilities

- `GET /health` returns a minimal process health response without authentication.
- `GET /api/status` returns structured service, Potato Hub, and Agent status with Bearer Token authentication.
- Configuration is loaded from environment variables through Pydantic Settings.
- Tests cover authentication, response shape, token secrecy, and missing token validation.

Current `/api/status` 中的 Potato Hub 和 Agent 状态是占位状态，不代表真实在线情况。

## Not Implemented Yet

- Real Hermes task APIs
- Feishu bot integration
- Codex CLI integration
- Database access
- MCP Server
- Video generation workflow
- Automatic deployment
- Tailscale Funnel or reverse proxy configuration

## Environment

This project uses Python 3.11+ and `uv`.

```bash
cd potato-gateway
uv sync
```

Create a strong token for local use:

```bash
export POTATO_GATEWAY_TOKEN="$(openssl rand -hex 32)"
```

Optional environment variables:

```bash
export POTATO_GATEWAY_HOST=127.0.0.1
export POTATO_GATEWAY_PORT=8765
export POTATO_GATEWAY_LOG_LEVEL=INFO
```

Copy `.env.example` only as a template. Do not commit a real `.env` or real token.

## Start

```bash
cd potato-gateway
uv run uvicorn potato_gateway.main:app \
  --host "${POTATO_GATEWAY_HOST:-127.0.0.1}" \
  --port "${POTATO_GATEWAY_PORT:-8765}"
```

You can also start it through the package entrypoint, which reads `POTATO_GATEWAY_HOST`, `POTATO_GATEWAY_PORT`, and `POTATO_GATEWAY_LOG_LEVEL` directly:

```bash
uv run potato-gateway
```

The default host is `127.0.0.1`. Do not change it to `0.0.0.0` unless the exposure path, authentication, logs, and documentation endpoints have been reviewed.

Swagger/OpenAPI docs remain enabled for local development. If this service is exposed publicly later through Tailscale Funnel, HTTPS reverse proxy, or another route, reevaluate whether `/docs`, `/redoc`, and `/openapi.json` should be disabled or protected.

## Verify

```bash
curl http://127.0.0.1:8765/health
```

```bash
curl http://127.0.0.1:8765/api/status \
  -H "Authorization: Bearer ${POTATO_GATEWAY_TOKEN}"
```

Missing or incorrect Bearer Tokens return `401 Unauthorized`.

## Test

```bash
cd potato-gateway
uv run pytest
```

## Security Notes

- Token is read only from `POTATO_GATEWAY_TOKEN`.
- Empty, short, and obvious placeholder tokens are rejected at startup.
- The service defaults to local-only `127.0.0.1`.
- Authentication uses constant-time token comparison.
- Request logs include method, path, status code, and latency only.
- Authorization headers, tokens, file paths, environment variables, and system details are not returned by API responses.
- No command execution, file reading, database, Hermes, Feishu, Codex, MCP, or video-generation endpoint is exposed in this phase.

Future HTTPS exposure should be handled with Tailscale Funnel or a reverse proxy, but this phase does not configure either.
