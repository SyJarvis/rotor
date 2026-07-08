# Rotor

Rotor is an API-only LLM gateway. It exposes OpenAI-compatible,
Anthropic-compatible, and basic OpenAI Responses-compatible endpoints over the
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

## Endpoints

- `GET /`
- `GET /api`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /anthropic/v1/messages`
- `GET /v1/models`
- `GET/POST/PUT/DELETE /api/admin/channels`
- `GET/POST/PUT/DELETE /api/admin/tokens`
- `GET /api/admin/logs`
