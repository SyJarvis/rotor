# Rotor

[简体中文](README.md) | [English](README.en.md)

Rotor is a self-hosted LLM API gateway. It organizes model providers and API
keys as channels, exposes unified OpenAI Chat Completions, OpenAI Responses,
OpenAI Images, and Anthropic Messages endpoints, and handles protocol
conversion, routing, fallback, token accounting, and runtime observability.

Rotor also provides a browser admin interface, administrative APIs, a
separately authenticated read-only Control API, and an independent
`rotor-mcp` diagnostics adapter.

## Key capabilities

- **Unified protocol endpoints**: `/v1/chat/completions`, `/v1/responses`,
  `/v1/images/generations`, and `/anthropic/v1/messages`.
- **Cross-protocol conversion**: basic messages, streaming events, tool calls,
  and usage data across OpenAI Chat, Responses, and Anthropic Messages.
- **Multi-channel routing**: channel selection by model, protocol, priority,
  weight, and runtime state, with fallback, conversation affinity, and adaptive
  scoring.
- **Observability**: request logs, individual channel attempts, normalized
  errors, latency, token usage, and cached tokens, exposed through the admin UI.
- **Administration and diagnostics**: manage channels and user tokens in the
  admin UI; expose read-only diagnostic facts to MCP, CLIs, and automation
  through the Control API.
- **Local-first storage**: SQLite by default, with PostgreSQL support and local
  conversation storage.

## How it works

```text
OpenAI / Anthropic SDKs, Codex, Claude Code, OpenCode
                           │
                           ▼
                      Rotor API
                           │
            Routing, conversion, fallback, accounting
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
 OpenAI-compatible     Responses        Anthropic
      channels          channels         channels

rotor-mcp ── Control API ──> Rotor diagnostic and usage facts
```

`rotor-mcp` is an independent Python package and process. It does not access
the Rotor database or import Rotor backend modules. The current Rotor Docker
image does not bundle or automatically start the MCP server.

## Quick start

### Requirements

- Python 3.11 or later
- SQLite (default) or PostgreSQL

### Install and run

```bash
git clone https://github.com/SyJarvis/rotor.git
cd rotor

python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

rotor serve --host 0.0.0.0 --port 8000
```

Check the service:

```bash
curl http://127.0.0.1:8000/health
```

Open the browser admin interface:

```text
http://127.0.0.1:8000/
```

Runtime data is stored under the following directory by default:

```text
~/.cache/rotor/
├── rotor.db
└── conversations/
```

## Docker

Run Rotor with SQLite:

```bash
docker build -t rotor .
mkdir -p "$HOME/.cache/rotor"

docker run --rm \
  -p 8000:8000 \
  -v "$HOME/.cache/rotor:/data" \
  rotor
```

The image stores SQLite at `/data/rotor.db` and the conversation store under
`/data/conversations/` by default. The single mount above persists both under
`~/.cache/rotor/` on the host.

Run Rotor and PostgreSQL with Docker Compose:

```bash
docker compose up --build -d
docker compose logs -f rotor
```

The repository Compose file uses development database credentials and exposes
the PostgreSQL port; change both before production deployment. Compose uses a
PostgreSQL volume for the primary database and mounts the conversation store
at `./data/conversations/` on the host. The current Docker configuration runs
Rotor only. `rotor-mcp` is intended to use a separate image.

## Make your first request

### 1. Add a channel

Open the admin interface and add the provider URL, API key, model, and
protocol under Channels. You can also call `POST /api/admin/channels`:

```bash
curl -X POST http://127.0.0.1:8000/api/admin/channels \
  -H "Content-Type: application/json" \
  -d '{
    "name": "openai-compatible",
    "type": "openai",
    "key": "provider-api-key",
    "base_url": "https://provider.example/v1",
    "models": ["your-model"],
    "model_mapping": {},
    "priority": 10,
    "weight": 1,
    "enabled": true,
    "protocol": "openai"
  }'
```

`protocol` describes the API exposed by the upstream:

| Value | Upstream API |
| --- | --- |
| `openai` | Chat Completions |
| `openai_responses` | Responses |
| `anthropic` | Anthropic Messages |

The provider type controls authentication. The protocol controls request paths
and conversion behavior.

### 2. Create a Rotor user token

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/admin/tokens/generate?name=demo&quota=1000000"
```

Save the returned `sk-` token:

```bash
export ROTOR_API_KEY="sk-your-rotor-token"
```

### 3. Call the unified API

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $ROTOR_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Conversation-Id: demo-conversation" \
  -d '{
    "model": "your-model",
    "messages": [
      {"role": "user", "content": "Reply with ROTOR_OK only"}
    ]
  }'
```

See [Client integrations](gitbook/guides/client-integrations.md) for Codex,
Claude Code, and OpenCode configuration.

## API overview

| Area | Main endpoints |
| --- | --- |
| Service status | `GET /health`, `GET /api` |
| OpenAI Chat | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` and Response resource endpoints |
| OpenAI Images | `POST /v1/images/generations` |
| Anthropic | `POST /anthropic/v1/messages`, `POST /anthropic/v1/messages/count_tokens` |
| Models | `GET /v1/models` |
| Admin API | `/api/admin/channels`, `/api/admin/tokens`, `/api/admin/logs`, `/api/admin/settings` |
| Control API | `/api/control/v1/channels`, request traces, recent failures, and model usage |

The Responses API supports creation, retrieval, cancellation, deletion, input
item listing, input-token counting, and compaction. For native Responses
channels, Rotor routes subsequent resource operations back to the originating
channel and account.

## Routing strategies

Rotor currently provides:

- `priority_weighted`: the default; priority is a hard boundary, with weighted
  selection inside the same priority.
- `fallback_order`: try compatible channels in deterministic order.
- `weighted`: weighted selection across compatible channels.
- `adaptive`: dynamically score channels within the same priority using
  success rate, latency, and current load.

Inspect and update runtime settings through `GET/PUT /api/admin/settings`.
Adaptive statistics are process-local and relearned after restart, while
request outcomes and routing decisions are persisted.

## Control API, MCP, and MindAgent

The Control API uses a dedicated Bearer token and does not accept ordinary
`sk-` user tokens:

```bash
export ROTOR_CONTROL_API_TOKEN="$(
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
```

Default read-only scopes:

```text
channel:read
request_trace:read
usage:read
```

`mcp/` is the independent `rotor-mcp` package. It currently exposes channel,
request trace, recent failure, and model usage queries over stdio. See the
[Rotor MCP README](mcp/README.md) for installation, startup, and client
configuration.

The MindAgent chat in the admin UI can run independently. It loads Rotor MCP
diagnostic tools only when an MCP command and Control token are configured.

## Important configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///~/.cache/rotor/rotor.db` | Primary database |
| `CONVERSATION_STORE_DIR` | `~/.cache/rotor/conversations` | Conversation storage |
| `LOG_LEVEL` | `INFO` | Logging; `DEBUG` also enables API documentation |
| `ROTOR_CONTROL_API_TOKEN` | unset | Control API and MCP authentication |
| `ROTOR_CONTROL_API_SCOPES` | three read-only scopes | Control API permissions |
| `ROTOR_MINDAGENT_MCP_COMMAND` | unset | Command used by MindAgent to start an independent MCP server |

See [Runtime configuration](gitbook/operations/configuration.md) for the full
reference.

## Security boundaries

- `/api/admin/*` currently has no independent administrator authentication.
  Keep it on a trusted network or behind an authenticated reverse proxy.
- Treat provider API keys, Rotor user tokens, the Control token, databases, and
  conversation files as sensitive data.
- The Control API and ordinary model requests use different tokens.
- Channel capability probes may make billable upstream generation calls.
  Ordinary model-list probes only call `GET /models`.

Read [Security boundaries](gitbook/operations/security.md) before deployment.

## Development and tests

```bash
python -m pip install -r requirements.txt
python -m pip install -e .
pytest -q
```

MCP tests:

```bash
cd mcp
python -m pip install -e ".[test]"
pytest -q
```

More documentation:

- [Full documentation](gitbook/README.md)
- [Installation](gitbook/getting-started/installation.md)
- [Protocols and conversion](gitbook/concepts/protocols.md)
- [Routing and fallback](gitbook/concepts/routing-and-fallback.md)
- [Logs, usage, and storage](gitbook/operations/observability.md)
- [Database migrations](gitbook/development/database-migrations.md)
- [Project roadmap](docs/roadmap.md)

## License

[MIT](LICENSE)
