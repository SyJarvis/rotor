# Codex、Claude Code 与 OpenCode 接入

## 接入前检查

先启动 Rotor，创建用户 Token，并确认目标模型可见：

```bash
export ROTOR_BASE_URL=http://127.0.0.1:8000
export ROTOR_API_KEY=sk-your-rotor-token
export ROTOR_MODEL=your-model

curl "$ROTOR_BASE_URL/health"
curl "$ROTOR_BASE_URL/v1/models" \
  -H "Authorization: Bearer $ROTOR_API_KEY"
```

Codex 和 OpenCode Responses 模式需要目标模型至少有一个
`protocol=openai_responses` 的原生渠道。Claude Code 可以通过 Rotor 的协议转换使用
OpenAI Chat、Responses 或 Anthropic 渠道；若需要完整 thinking、prompt caching 和
Anthropic 原生扩展，优先使用 `protocol=anthropic` 渠道。原生渠道的 token
counting 会直接代理到上游；跨协议渠道会返回稳定的本地估算值。

## Codex CLI

复制模板到 Codex 用户配置目录，并替换 `your-model`：

```bash
cp examples/client-configs/codex-rotor.config.toml ~/.codex/rotor.config.toml
export ROTOR_API_KEY=sk-your-rotor-token
codex --profile rotor
```

非交互冒烟测试：

```bash
codex exec --profile rotor "只回复 ROTOR_OK，不要调用工具"
```

Codex 的 provider 配置必须位于 `~/.codex/config.toml` 或同目录的 profile
配置中；项目内 `.codex/config.toml` 不能覆盖 `model_provider` 和
`model_providers`。

## Claude Code

Rotor 的 Anthropic 客户端入口是 `/anthropic/v1/messages`，因此
`ANTHROPIC_BASE_URL` 应设置到 `/anthropic`，Claude Code 会自动追加
`/v1/messages`：

```bash
export ROTOR_API_KEY=sk-your-rotor-token
export ROTOR_MODEL=your-model
source examples/client-configs/claude-code.env

claude --model "$ROTOR_MODEL"
```

非交互冒烟测试：

```bash
claude -p --model "$ROTOR_MODEL" --tools "" \
  "只回复 ROTOR_OK"
```

这里使用 `ANTHROPIC_AUTH_TOKEN`，Claude Code 会把它作为 Bearer Token 发送，
Rotor 使用同一个用户 Token 完成鉴权。

## OpenCode

Responses 原生模式：

```bash
# 先替换模板中的三处 your-model
export OPENCODE_CONFIG="$PWD/examples/client-configs/opencode-responses.json"
export ROTOR_API_KEY=sk-your-rotor-token
opencode run "只回复 ROTOR_OK"
```

如果目标模型只有 Chat Completions 渠道，使用：

```bash
# 先替换模板中的三处 your-model
export OPENCODE_CONFIG="$PWD/examples/client-configs/opencode-chat.json"
export ROTOR_API_KEY=sk-your-rotor-token
opencode run "只回复 ROTOR_OK"
```

Responses 配置使用 `@ai-sdk/openai`；Chat Completions 配置使用
`@ai-sdk/openai-compatible`。两者不要混用，否则 OpenCode 会请求错误的上游路径。

## Session Lease 行为

Rotor 会识别 Codex、Claude Code 和 OpenCode 的稳定 Session 信号。同一 Rotor Token、
Session 和 logical model 的成功请求会形成持久化 Channel 租约；fallback 成功后，后续
turn 会保持在新 Channel，直到空闲 TTL 过期。客户端仍负责发送对话历史；租约只管理
路由，不保存或迁移 KV Cache。使用原生 Responses `previous_response_id` 时，对象始终
返回创建它的上游 Channel。

## 常见错误

- `401`：检查 Token 是否以 Rotor 配置的 `API_KEY_PREFIX` 开头，且未禁用或过期。
- `404 /v1/messages`：Claude Code 的 Base URL 少了 `/anthropic`。
- `No native Responses channel`：Codex/OpenCode Responses 所用模型没有
  `openai_responses` 渠道。
- 工具调用没有发生：确认所选模型和渠道的 `function_call` capability 已通过管理端探测。
- 模型找不到：客户端模型名必须出现在渠道 `models` 或 `model_mapping` 的键中。
