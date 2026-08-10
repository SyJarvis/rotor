# 接入 Codex、Claude Code 与 OpenCode

本页给出仓库随附配置模板的使用方式。开始前先启动 Rotor、创建用户 Token，并
确认目标模型可见：

```bash
export ROTOR_BASE_URL=http://127.0.0.1:8000
export ROTOR_API_KEY=sk-your-rotor-token
export ROTOR_MODEL=your-model

curl "$ROTOR_BASE_URL/health"
curl "$ROTOR_BASE_URL/v1/models" \
  -H "Authorization: Bearer $ROTOR_API_KEY"
```

## Codex CLI

仓库模板为 `examples/client-configs/codex-rotor.config.toml`。将其内容合并到
Codex 用户配置，替换 `your-model`，然后：

```bash
export ROTOR_API_KEY=sk-your-rotor-token
codex --profile rotor
```

冒烟测试：

```bash
codex exec --profile rotor "只回复 ROTOR_OK，不要调用工具"
```

Codex 使用 Responses API。需要原生 Responses 独有能力时，目标模型必须具有
`protocol=openai_responses` 渠道。

## Claude Code

Claude Code 会在 Base URL 后追加 `/v1/messages`，因此 Base URL 应指向
Rotor 的 `/anthropic` 前缀。仓库模板为 `examples/client-configs/claude-code.env`：

```bash
export ROTOR_API_KEY=sk-your-rotor-token
export ROTOR_MODEL=your-model
source examples/client-configs/claude-code.env

claude --model "$ROTOR_MODEL"
```

冒烟测试：

```bash
claude -p --model "$ROTOR_MODEL" --tools "" "只回复 ROTOR_OK"
```

跨协议渠道支持 Messages 基础内容和工具调用转换。若依赖完整 thinking、prompt
caching 或 Anthropic 私有扩展，应使用 `protocol=anthropic` 的原生渠道。

## OpenCode

原生 Responses 模式：

```bash
export OPENCODE_CONFIG="$PWD/examples/client-configs/opencode-responses.json"
export ROTOR_API_KEY=sk-your-rotor-token
opencode run "只回复 ROTOR_OK"
```

仅有 Chat Completions 渠道时：

```bash
export OPENCODE_CONFIG="$PWD/examples/client-configs/opencode-chat.json"
export ROTOR_API_KEY=sk-your-rotor-token
opencode run "只回复 ROTOR_OK"
```

运行前替换模板中的 `your-model`。Responses 模板使用 `@ai-sdk/openai`，Chat
模板使用 `@ai-sdk/openai-compatible`，两种配置不能混用。

遇到路径、模型或原生协议错误时，查看[常见错误](../troubleshooting/common-errors.md)。
