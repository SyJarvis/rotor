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
  scoring; same-protocol channels are preferred (configurable), and requests
  needing native semantics are never silently downgraded.
- **Observability**: request logs, individual channel attempts, normalized
  errors, latency, token usage, and cached tokens; the admin **Monitoring** page
  shows process-local performance samples, client sources, and incremental
  reconciliation.
- **Administration and diagnostics**: manage channels and user tokens in the
  admin UI; expose read-only diagnostic facts to MCP, CLIs, and automation
  through the Control API.
- **Local-first storage**: file-backed SQLite, with local conversation storage.

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
- SQLite (this release supports file-backed SQLite only)

### Install and run

```bash
git clone https://github.com/SyJarvis/rotor.git
cd rotor

python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

rotor serve --host 127.0.0.1 --port 8000
```

Check the service:

```bash
curl http://127.0.0.1:8000/health
```

Open the browser admin interface:

```text
http://127.0.0.1:8000/
```

On first startup Rotor creates the administrator `admin` with password
`123456`. The first login must change that password before any management API
can be used. Complete this step locally before binding Rotor to a non-local
network interface.

Runtime data is stored under the following directory by default:

```text
~/.cache/rotor/
├── rotor.db
├── conversations/
└── logs/
    └── YYYY-MM/
        └── YYYY-MM-DD.log
```

## Docker

Run Rotor with SQLite:

```bash
docker build -t rotor .
mkdir -p "$HOME/.cache/rotor"

docker run --rm \
  -p 127.0.0.1:8000:8000 \
  -v "$HOME/.cache/rotor:/data" \
  rotor
```

The image stores SQLite at `/data/rotor.db`, the conversation store under
`/data/conversations/`, and runtime logs under `/data/logs/` by default. The
single mount above persists all of them under `~/.cache/rotor/` on the host.

Run Rotor with Docker Compose and keep runtime data in `./data`:

```bash
docker compose up --build -d
docker compose logs -f rotor
```

The repository Compose file uses SQLite, binds the port to `127.0.0.1` only, and
mounts the database, conversation store, and runtime logs under `./data/` on the
host. The current Docker configuration runs Rotor only. `rotor-mcp` is intended
to use a separate image.

## Make your first request

### 1. Add a channel

Sign in to the admin interface and add the provider URL, API key, model, and
protocol under Channels. HTTP automation must first establish an authenticated
admin session as described in [Admin API](gitbook/reference/admin-api.md).

`protocol` describes the API exposed by the upstream:

| Value | Upstream API |
| --- | --- |
| `openai` | Chat Completions |
| `openai_responses` | Responses |
| `anthropic` | Anthropic Messages |

The provider type controls authentication. The protocol controls request paths
and conversion behavior.

### 2. Create a Rotor user token

Generate a Rotor user token from **API Keys** in the admin interface.

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
| Anthropic | `POST /anthropic/v1/messages`, `POST /anthropic/v1/messages/count_tokens`, `GET /anthropic/v1/models` |
| Models | `GET /v1/models` |
| Admin API | `/api/admin/channels`, `/api/admin/tokens`, `/api/admin/logs`, `/api/admin/settings` |
| Monitoring | `/api/admin/monitoring/sources`, `/api/admin/monitoring/performance`, `/api/admin/monitoring/reconciliation` |
| Control API | `/api/control/v1/channels`, request traces, recent failures, model usage, and Session Lease evaluation |

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

Candidate generation is then bounded by two more rules: capability filtering
(hard) drops channels that cannot express the request semantics, and
`routing.protocol_affinity_enabled` (on by default) orders same-family channels
ahead of the ones that require protocol conversion. Requests needing native
Responses or Anthropic semantics are never silently downgraded.

Inspect and update runtime settings through `GET/PUT /api/admin/settings`.
Adaptive statistics and channel cooldowns are process-local and relearned after
restart, while request outcomes and routing decisions are persisted.

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
request trace, recent failure, model usage, and Session Lease fact evaluation
over stdio. The evaluation only determines whether the facts are complete
enough for further analysis; it neither recommends nor automatically enables
cost-aware routing. See the [Rotor MCP README](mcp/README.md) for installation,
startup, and client configuration.

Besides a fixed Control token from the environment, the admin UI can create a
database-backed `rck_` credential under **API Keys → MCP Control Keys**. The
full value is shown only once, can be disabled or deleted independently, and
does not accept ordinary `sk-` user tokens.

The MindAgent chat in the admin UI can run independently. It loads Rotor MCP
diagnostic tools only when an MCP command and Control token are configured.
Its local conversation history can be created, exported as JSON, or deleted
from the sidebar.

## Important configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///~/.cache/rotor/rotor.db` | Primary database |
| `CONVERSATION_STORE_DIR` | `~/.cache/rotor/conversations` | Conversation storage |
| `ROTOR_LOG_DIR` | `~/.cache/rotor/logs` | Runtime logs, grouped into monthly directories and daily files |
| `LOG_LEVEL` | `INFO` | Logging; `DEBUG` also enables API documentation |
| `ROTOR_DEFAULT_ADMIN_USERNAME` | `admin` | Administrator username used only when initializing an empty database |
| `ROTOR_DEFAULT_ADMIN_PASSWORD` | `123456` | Initial password; the first login must replace it |
| `ROTOR_ADMIN_SESSION_IDLE_SECONDS` | `1800` | Admin session idle timeout in seconds |
| `ROTOR_ADMIN_SESSION_TTL_SECONDS` | `43200` | Admin session absolute lifetime in seconds |
| `ROTOR_ADMIN_COOKIE_SECURE` | `false` | Must be `true` for HTTPS deployments |
| `ROTOR_ADMIN_LOGIN_MAX_FAILURES` | `5` | Failed-login threshold |
| `ROTOR_ADMIN_LOGIN_WINDOW_SECONDS` | `300` | Failed-login counting window in seconds |
| `ROTOR_ADMIN_LOGIN_LOCK_SECONDS` | `900` | Lockout duration in seconds |
| `ROTOR_CONTROL_API_TOKEN` | unset | Control API and MCP authentication |
| `ROTOR_CONTROL_API_SCOPES` | three read-only scopes | Control API permissions |
| `ROTOR_MINDAGENT_MCP_COMMAND` | unset | Command used by MindAgent to start an independent MCP server |

See [Runtime configuration](gitbook/operations/configuration.md) for the full
reference.

## Security boundaries

- `/api/admin/*` uses administrator sessions and CSRF protection. Replace the
  default password before exposing Rotor outside the local machine.
- Treat provider API keys, Rotor user tokens, the Control token, databases, and
  conversation files as sensitive data.
- The Control API and ordinary model requests use different tokens.
- Channel capability probes may make billable upstream generation calls.
  Ordinary model-list probes only call `GET /models`.

Read [Security boundaries](gitbook/operations/security.md) before deployment.

## Development and tests

```bash
python -m pip install -e .
python -m pip install pytest pytest-asyncio openai anthropic
pytest -q
```

Frontend tests use Node:

```bash
node --experimental-vm-modules --test tests/test_*.mjs
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

## License

[MIT](LICENSE)
