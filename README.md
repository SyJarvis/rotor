# Rotor

Rotor is an API-only LLM gateway. It exposes OpenAI-compatible,
Anthropic-compatible, and OpenAI Responses-compatible endpoints over the
models and provider keys configured as channels.

Rotor includes a minimal browser admin page at `/`. Management can also be
done directly through HTTP admin APIs.

## Run

```bash
cd /Users/whoami/research/apirouter/rotor
pip install -e .
rotor serve --host 0.0.0.0 --port 8000
```

Open the admin page:

```text
http://localhost:8000
```


By default, Rotor stores runtime files in `~/.cache/rotor`:

```text
~/.cache/rotor/
  rotor.db
  conversations/
```

## Important Environment

```env
# Optional. Defaults to sqlite+aiosqlite:///~/.cache/rotor/rotor.db
DATABASE_URL=
# Optional. Defaults to ~/.cache/rotor/conversations
CONVERSATION_STORE_DIR=
```

## Configure Channels

Each channel stores one provider base URL, provider API key, supported models,
priority, and weight.

```bash
curl -X POST http://localhost:8000/api/admin/channels \
  -H "Content-Type: application/json" \
  -d '{
    "name": "zhipu-main",
    "type": "zhipu",
    "key": "provider-api-key",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "models": ["glm-4.7"],
    "model_mapping": {},
    "priority": 10,
    "weight": 1,
    "enabled": true,
    "protocol": "openai"
  }'
```

`base_url` is the upstream provider API root. `key` is that provider's API key.
Different providers or different keys should be configured as separate
channels. Channels serving the same model are distributed by priority and
weight.

Set `protocol` to `openai_responses` for an upstream that exposes native
`POST /responses`. Native Responses channels preserve hosted tools, multimodal
items, background jobs, response state, resource lifecycle calls, input-token
counting, compaction, and Responses SSE events. Rotor persistently binds each
native response ID to its originating channel and API token, so retrieval,
cancellation, deletion, input-item listing, and `previous_response_id` requests
return to the correct upstream account. Function tools can also be converted
across `openai`, `openai_responses`, and `anthropic` channels.

Channel connectivity tests use `GET /models` and do not consume model tokens.
An administrator can explicitly run a generation capability probe (which may
be billable):

```bash
curl -X POST \
  "http://localhost:8000/api/admin/channels/1/test?capability=function_call&test_model=your-model"
```

## Adaptive Routing

Rotor can rank otherwise-compatible channels using online success, latency,
and in-flight load signals. The scoring interface also reserves a cost signal,
which remains neutral until model pricing is configured. Administrative
priority remains a hard tier: adaptive scoring only reorders channels within
the same priority. The default strategy remains `priority_weighted`; enable the
first explainable policy with:

```bash
curl -X PUT http://localhost:8000/api/admin/settings \
  -H "Content-Type: application/json" \
  -d '{
    "routing": {
      "strategy": "adaptive",
      "affinity_enabled": true
    }
  }'
```

Every generated decision stores its candidate set, feature snapshot, policy
version, and score snapshot in `routing_decisions`. Request outcomes continue
to be stored in `usage_ledger`; the tables join on `request_id` and form the
training source for future offline models. Online statistics are process-local
in this first version and reset on restart, while the training data persists.

## Generate User API Key

```bash
curl -X POST "http://localhost:8000/api/admin/tokens/generate?name=demo&quota=1000000" \
```

Use the returned key against Rotor:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $ROTOR_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Conversation-Id: conv_demo" \
  -d '{
    "model": "glm-4.7",
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

Image generation is exposed through the OpenAI-compatible Images API. The
channel can remain `protocol=openai_responses`; Rotor uses the dedicated
upstream `/images/generations` path for this endpoint. Override it with
`extra.images_path` only when a provider uses a non-standard path.

```bash
curl -X POST http://localhost:8000/v1/images/generations \
  -H "Authorization: Bearer $ROTOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "A small red house beside a quiet lake",
    "size": "1024x1024"
  }'
```

Codex CLI、Claude Code 和 OpenCode 的配置模板与冒烟命令见
[客户端接入指南](docs/client-integrations.md)。

## Endpoints

- `GET /`
- `GET /api`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/images/generations`
- `GET/DELETE /v1/responses/{response_id}`
- `POST /v1/responses/{response_id}/cancel`
- `GET /v1/responses/{response_id}/input_items`
- `POST /v1/responses/input_tokens`
- `POST /v1/responses/compact`
- `POST /anthropic/v1/messages`
- `POST /anthropic/v1/messages/count_tokens`
- `GET /v1/models`
- `GET/POST/PUT/DELETE /api/admin/channels`
- `GET/POST/PUT/DELETE /api/admin/tokens`
- `GET /api/admin/logs`
