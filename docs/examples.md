# 调用示例

## 1. 生成用户 API Key

```bash
curl -X POST "http://localhost:8000/api/admin/tokens/generate?name=demo&quota=1000000"
```

返回的 `key` 即用户后续调用 Rotor 用的 API Key。

## 2. OpenAI Chat 协议

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

## 3. Anthropic Messages 协议

```bash
curl -X POST http://localhost:8000/anthropic/v1/messages \
  -H "x-api-key: $ROTOR_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.7",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

## 4. Python SDK 示例

仓库下 `examples/` 目录提供了基于官方 SDK 的完整示例:

- [`openai_example.py`](https://github.com/SyJarvis/rotor/blob/main/examples/openai_example.py) — OpenAI SDK 调用
- [`openai_responses_example.py`](https://github.com/SyJarvis/rotor/blob/main/examples/openai_responses_example.py) — Responses API 调用
- [`anthropic_messages_example.py`](https://github.com/SyJarvis/rotor/blob/main/examples/anthropic_messages_example.py) — Anthropic SDK 调用
