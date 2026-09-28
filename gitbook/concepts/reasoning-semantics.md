# 三种协议的推理强度语义

本文说明 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 中与推理
相关的字段、语义和 Rotor 的处理边界。本文不是跨供应商的统一标准；相同字符串（例如
`high` 或 `max`）在不同模型和协议中不保证行为相同。

## 结论先行

| 协议 | 主要控制字段 | 控制的对象 | Rotor 是否自动转换到其他协议 |
| --- | --- | --- | --- |
| OpenAI Responses | `reasoning.effort` | OpenAI 模型在请求中投入的推理强度；可用值由模型决定 | 可转换到 OpenAI Chat 的 `reasoning_effort` 语义，但当前 Responses 请求要求 native |
| OpenAI Chat Completions | `reasoning_effort` | OpenAI Chat 请求的推理强度；可用值由模型决定 | 目标是 Responses 时转换为 `reasoning.effort` |
| Anthropic Messages | `thinking` + `output_config.effort` | `thinking` 控制 thinking 模式；`effort` 控制整体输出工作量 | 不自动转换为 OpenAI 的推理字段 |

因此，统一客户端配置中的 `max` 只能表示“客户端希望使用较高/最高档位”，不能证明
三个上游协议的实际推理行为完全等价。最终是否接受某个值、默认值是什么，以及该值对
质量、延迟和 Token 的影响，都必须以目标模型的文档为准。

## 1. OpenAI Responses

### 1.1 请求字段

Responses 请求使用嵌套字段：

```json
{
  "model": "gpt-5.6-luna",
  "input": "解释这个错误",
  "reasoning": {
    "effort": "high"
  }
}
```

OpenAI 将 `reasoning.effort` 定义为指导模型在任务上投入多少思考的参数。支持的值
取决于模型，而不是由 Responses 协议统一固定。以 GPT-5.6 Luna 的官方模型资料为例，
当前列出的值是：

```text
none, low, medium, high, xhigh, max
```

该模型页面列出的默认值是 `medium`。这不能推导出其他 OpenAI 模型或第三方兼容模型
也支持完全相同的集合。

官方资料：

- [OpenAI Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
- [GPT-5.6 Luna model](https://developers.openai.com/api/docs/models/gpt-5.6-luna)

### 1.2 语义边界

`effort` 是行为控制信号，不是严格的“必须消耗多少 reasoning tokens”的预算。较高
强度通常意味着模型可以进行更充分的推理，但具体 Token 使用和延迟仍由模型、任务和
服务实现决定。

`none` 也不是所有模型都支持；不能把它解释为所有模型通用的“关闭思考”。例如某些
模型不接受 `none`，发送后会返回参数错误。

Responses 还可以通过 `reasoning.summary` 请求 reasoning summary，但 summary 是输出
可见性/摘要控制，不等于推理强度；它不应被当成 `effort` 的别名。

## 2. OpenAI Chat Completions

### 2.1 请求字段

Chat Completions 使用顶层字段：

```json
{
  "model": "gpt-5.6-luna",
  "messages": [
    {"role": "user", "content": "解释这个错误"}
  ],
  "reasoning_effort": "high"
}
```

OpenAI 官方推理文档同时说明了 Chat Completions 的 `reasoning_effort` 和 Responses
的 `reasoning.effort`。但是两者的可用值仍然由目标模型决定；Chat 字段名相同不代表
所有 Chat-compatible provider 都实现了相同语义。

### 2.2 Rotor 的处理

当 Chat 请求发送到 Chat 上游时，Rotor 保留：

```json
"reasoning_effort": "high"
```

当 Chat 请求发送到 Responses 上游时，Rotor 转换为：

```json
"reasoning": {
  "effort": "high"
}
```

Rotor 当前支持的兼容别名是：

| Chat 输入 | Responses 上游输出 |
| --- | --- |
| `off` | `none` |
| `ultra` | `max` |
| `ultracode` | `max` |
| `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max` | 原样保留 |

`off`、`ultra` 和 `ultracode` 是 Rotor 的客户端兼容别名，不是 OpenAI Chat 或
Responses 协议标准值。Rotor 当前不会对未知字符串做统一枚举校验，未知值会继续交给
上游验证。

Chat 请求的 `reasoning_effort` 会参与能力路由，配置了能力白名单的 Channel 需要声明
相应的 `reasoning_effort` 能力。该能力表示“可以承接这个 Chat 字段”，不表示所有
等级都被目标模型支持。

## 3. Anthropic Messages

Anthropic 的推理语义与 OpenAI 不同，它把两个问题分开：

1. 是否启用 thinking，以及使用哪种 thinking 模式，由 `thinking` 控制；
2. 整体输出工作量/推理深度，由 `output_config.effort` 控制。

### 3.1 Adaptive thinking

当前支持 adaptive thinking 的模型可以使用：

```json
{
  "model": "claude-opus-5",
  "max_tokens": 4096,
  "thinking": {
    "type": "adaptive"
  },
  "output_config": {
    "effort": "high"
  },
  "messages": [
    {"role": "user", "content": "分析这个问题"}
  ]
}
```

这里：

- `thinking.type="adaptive"` 表示使用 adaptive thinking 模式；
- `output_config.effort` 控制 Claude 整体投入多少工作，包括 thinking（启用时）、文本
  输出和工具调用；
- `effort` 是行为信号，不是严格的 thinking token 预算；
- 较低 effort 可能让简单任务跳过 thinking，但不能把 `low` 简化成 OpenAI 的 `none`。

Anthropic 当前 Effort 文档列出的等级可能包括：

```text
low, medium, high, xhigh, max
```

具体模型支持的集合不同，且默认值也可能不同。官方文档当前描述 `high` 是常见默认
行为，但应用不能据此替代目标模型的兼容性检查。

官方资料：

- [Anthropic Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Anthropic Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)

### 3.2 Legacy extended thinking

较早的 Claude 模型使用固定预算的 extended thinking：

```json
{
  "thinking": {
    "type": "enabled",
    "budget_tokens": 10000
  }
}
```

`budget_tokens` 是 thinking 预算目标，不是 `effort` 等级。它必须满足目标模型的
预算规则，并且会受到 `max_tokens` 等限制。Anthropic 当前文档将这种模式视为 legacy：
不同 Claude 版本对 `type="enabled"` 的支持不同，较新的模型应使用 adaptive thinking。

因此，下列字段不能互相直接替换：

```text
OpenAI reasoning.effort       ≠ Anthropic output_config.effort
OpenAI reasoning.effort=none  ≠ Anthropic thinking disabled
Anthropic budget_tokens       ≠ effort level
thinking.type=adaptive        ≠ effort level
```

## 4. 三种协议的比较

### 4.1 “等级”不是跨协议标准

即使三个协议都出现 `low`、`medium`、`high` 或 `max`，它们也只是在各自协议内有意义：

| 比较项 | OpenAI Responses/Chat | Anthropic Messages |
| --- | --- | --- |
| 强度字段 | `reasoning.effort` / `reasoning_effort` | `output_config.effort` |
| thinking 开关 | 通常由模型和 effort 语义决定 | 单独由 `thinking` 控制 |
| 固定 token 预算 | 不是 `effort` 的定义 | legacy `budget_tokens` 可提供预算目标 |
| 可用等级 | 按模型决定 | 按模型决定 |
| 默认值 | 按模型决定 | 按模型决定 |
| 同名等级是否等价 | 不保证 | 不保证 |

### 4.2 建议的统一层语义

如果客户端必须支持多个供应商，建议把客户端配置称为“目标 effort 档位”，而不是
“统一推理标准”，并在 provider adapter 层做明确映射：

```text
客户端档位
  ├─ OpenAI Responses → reasoning.effort
  ├─ OpenAI Chat      → reasoning_effort
  └─ Anthropic        → thinking + output_config.effort
```

只有在完成模型能力表和实际评测后，才可以决定例如：

```text
客户端 max → OpenAI max
客户端 max → Anthropic max
```

但这只能表示配置层的对应关系，不能声称两个供应商的计算量、延迟或质量相同。

## 5. Rotor 当前实现边界

### OpenAI 请求

- Responses 请求中的 `reasoning` 会保留在原始 `responses_payload`。
- Responses 请求包含 `reasoning` 时，当前路由要求 `responses_native`，不会静默降级到
  Chat 或 Anthropic。
- Chat→Responses 会将 `reasoning_effort` 写成 `reasoning.effort`。
- `off`、`ultra`、`ultracode` 只在 OpenAI 风格转换路径中做别名映射。

### Anthropic 请求

- `AnthropicMessageRequest` 允许扩展字段；原生 Anthropic adapter 会保留
  `thinking`、`output_config` 及其他原生字段。
- 含 `thinking`、`output_config` 等原生语义的 Anthropic 请求会要求
  `anthropic_native`。
- Rotor 不把 OpenAI `reasoning_effort` 自动转换为 Anthropic `thinking`，也不把
  Anthropic `budget_tokens` 反向转换成 OpenAI effort。
- Anthropic thinking 输出块在原生路径保留；跨协议转换对 thinking/redacted thinking
  有语义边界，不能承诺完整保留。

实现和测试入口：

- [`ResponsesRequest`](../../src/rotor/schemas/responses.py)
- [`ChatCompletionRequest`](../../src/rotor/schemas/request.py)
- [`AnthropicMessageRequest`](../../src/rotor/schemas/request.py)
- [`Responses adapter`](../../src/rotor/adapters/protocol/responses.py)
- [`Anthropic adapter`](../../src/rotor/adapters/protocol/anthropic.py)
- [`request capabilities`](../../src/rotor/gateway/capabilities.py)
- [`Responses protocol tests`](../../tests/test_responses_protocol.py)
- [`Anthropic protocol tests`](../../tests/test_anthropic_protocol.py)

## 6. 相关资料

- [协议兼容与转换](protocols.md)
- [三种协议格式与转换手册](protocol-formats.md)
- [Channel 字段与能力](../reference/channel-schema.md)
- [OpenAI Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
- [GPT-5.6 Luna model](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [Anthropic Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Anthropic Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)
- [Anthropic Extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
