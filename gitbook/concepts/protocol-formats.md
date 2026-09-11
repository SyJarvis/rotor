# 三种消息协议格式与 Rotor 转换手册

## 适用读者与快照边界

本文面向实现客户端、配置 Channel、排查 SSE 或编写适配器的开发者。它描述的是 Rotor 工作树在 2026-09-02（Asia/Shanghai）可观察到的协议子集，不是 OpenAI 或 Anthropic 的完整官方规范。代码、模型和测试是事实来源；当本文与其他说明冲突时，应以当前源码为准。

代码快照 HEAD 为 `7a15ecbe8eaf58ad33c9a1847325ec1b06017540`。当前工作树存在其他既有未提交修改和未跟踪文件；本手册只描述协议相关的可见实现，不表示这些改动已经发布。

本文覆盖三个消息协议：

1. OpenAI Chat Completions（简称 Chat）；
2. OpenAI Responses（简称 Responses）；
3. Anthropic Messages（简称 Anthropic）。

Images 是独立的第四条请求路径（`/v1/images/generations`），不经过下文的消息协议
转换。入口说明见[客户端 API](../reference/client-api.md)。

源码快照和证据索引见[源码/测试索引](#源码与测试索引)。示例中的字段是 Rotor 当前实现会读取、生成或保留的字段；供应商可能支持更多字段，不能据此推断 Rotor 会接受或转发这些字段。

## 1. 两层协议模型

### 1.1 `request_protocol`：客户端契约

路由入口为每个请求固定一个 `request_protocol`：

| HTTP 入口 | `request_protocol` | 客户端看到的响应 |
| --- | --- | --- |
| `POST /v1/chat/completions` | `openai_chat` | Chat JSON 或 Chat SSE |
| `POST /v1/responses` | `openai_responses` | Responses JSON 或 Responses SSE |
| `POST /anthropic/v1/messages` | `anthropic_messages` | Anthropic JSON 或 Anthropic SSE |

这个值用于能力筛选、路由记录、attempt/usage 记账和错误包装；它不会因为选择了某个
上游 Channel 而改变。Chat 路由在 [`chat_completions`](../../src/rotor/api/v1/chat.py)
设置它，Responses 路由在 [`create_response`](../../src/rotor/api/v1/responses.py)
设置它，Anthropic 路由在 [`messages`](../../src/rotor/api/v1/anthropic.py)
设置它。

### 1.2 `Channel.protocol`：上游 wire protocol

Channel 的 `protocol` 决定适配器向 provider 发出的 URL、鉴权、请求 JSON 和解析方式：

| `Channel.protocol` | 适配器 | 常见默认路径 | 常见默认鉴权 |
| --- | --- | --- | --- |
| `openai` | `OpenAICompatibleAdapter` 的具体实现 | `/chat/completions` | `Authorization: Bearer` |
| `openai_responses`（或 `responses`） | `OpenAIResponsesAdapter` | `/responses` | `Authorization: Bearer` |
| `anthropic`（或 `anthropic_messages`） | `AnthropicAdapter` | `/messages` | `x-api-key`，并附 `anthropic-version` |

上述是常见默认值，最终以 provider preset、`extra.request_path`、`extra.auth_type` 和
`extra.headers` 为准；`model_mapping` 只映射
logical model 到 provider model。默认推导和请求头见
[`presets.py`](../../src/rotor/channels/presets.py) 与
[`base.py`](../../src/rotor/adapters/base.py)。

例如 `zhipu` + Anthropic protocol 仍可使用 Bearer，而 `anthropic` type + Responses
protocol 可按 `auth_type` 使用 `x-api-key`；组合不能只看协议名或 provider 名。

因此，同一个客户端入口可以选择不同 wire protocol。例如 Chat 请求经过
`Channel(protocol="openai_responses")` 时，客户端仍然收到 Chat 格式，只有上游是
Responses；反之 Responses 客户端也可在基础字段可表达时选择 Chat 或 Anthropic 上游。

### 1.3 provider type、preset 与 adapter

`Channel.type` 先用于 provider preset 和 adapter registry；`Channel.protocol` 再决定
wire adapter。当前注册关系如下（Responses/Anthropic protocol 会优先覆盖 type）：

| type | 默认 preset/adapter |
| --- | --- |
| `openai`、`deepseek` | OpenAI-compatible，通常 `/chat/completions` |
| `azure` | `AzureOpenAIAdapter`；直接在 `channel.base_url` 后追加 `/chat/completions?api-version=2023-05-15`（base_url 通常已含 deployment），不完全遵循通用 path 规则 |
| `anthropic` | Anthropic adapter；常见 `/messages` + `x-api-key` |
| `moonshot`、`minimax`、`zhipu`、`kimi` | 各自 provider 子类或 OpenAI-compatible |

详见 [`AdapterFactory`](../../src/rotor/adapters/factory.py) 和
[`PROVIDER_PRESETS`](../../src/rotor/channels/presets.py)。

### 1.4 native、pass-through 与 cross-protocol

- **native/pass-through**：由具体 adapter 的 `native_responses`/`native_anthropic` 标志
  与对应 native payload 共同决定；不是简单比较字符串（例如 `openai_chat` 与
  `openai`、`anthropic_messages` 与 `anthropic` 本来就不同）。Responses 入口把
  `responses_payload` 交给 Responses adapter 时、Anthropic 入口把 `anthropic_payload`
  交给 Anthropic adapter 时，才走对应原生路径。
- **cross-protocol**：两者不同，先归一化为 Rotor 内部 `ChatCompletionRequest`，再由
  目标适配器转换。只承诺本文矩阵列出的可表达字段。
- Router 会在请求开始前按能力筛选候选；已经向客户端发出字节后不会把同一请求透明
  重放到另一 Channel。

## 2. OpenAI Chat Completions

### 2.1 客户端入口和请求模型

入口为 `POST /v1/chat/completions`，需要 Rotor Token，可使用
`Authorization: Bearer` 或 `x-api-key`。请求模型为
[`ChatCompletionRequest`](../../src/rotor/schemas/request.py)：`model` 和
`messages` 必填，其余字段为可选子集。

最小非流式请求：

```json
{
  "model": "gpt-5.6-sol",
  "messages": [{"role": "user", "content": "你好"}],
  "stream": false
}
```

当前可被模型验证器读取的主要请求字段：

| 字段 | 形状 | Rotor 行为 |
| --- | --- | --- |
| `model` | string | 逻辑模型名；之后按 Channel 映射 |
| `messages` | message[] | `system` 会被移动到稳定前缀 |
| `temperature`、`top_p` | number | 原样传给可表达的上游 |
| `n` | integer | Chat 上游字段；Responses/Anthropic 转换不保证保留 |
| `max_tokens` | integer | Responses 映射为 `max_output_tokens`；Anthropic 映射为 `max_tokens` |
| `presence_penalty`、`frequency_penalty` | number | 仅 Chat wire protocol 有直接对应 |
| `stop` | string 或 string[] | Anthropic 映射为 `stop_sequences` |
| `stream` | boolean | 选择 SSE；默认 false |
| `tools` | function tool[] | 转换为 Responses function tool 或 Anthropic tool |
| `tool_choice` | `none`/`auto`/`required`/function 对象 | 按目标协议映射 |
| `user` | string | Chat→Responses 保留；Chat→Anthropic 不发送 |

消息角色是 `system`、`user`、`assistant`、`tool`。content 可以是字符串或内容块
数组；当前转换器识别文本和 `image_url`。图片内容属于消息中的多模态块，Images
生成接口则是独立路径。

工具调用通过 `tools` function 定义、assistant `tool_calls` 和 `tool` 消息表达；
模型定义见 [`request.py`](../../src/rotor/schemas/request.py)，通用 Chat 适配器序列化
见 [`base.py`](../../src/rotor/adapters/base.py)。

### 2.2 非流式响应

成功响应是 `chat.completion` 子集，模型定义见
[`ChatCompletionResponse`](../../src/rotor/schemas/request.py)：

```json
{
  "id": "chatcmpl_abc",
  "object": "chat.completion",
  "created": 1720000000,
  "model": "gpt-5.6-sol",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "今天天气晴朗。"},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 8,
    "total_tokens": 20,
    "prompt_tokens_details": {"cached_tokens": 0}
  }
}
```

工具调用时，`message.content` 可以为 null，`message.tool_calls[]` 携带
`id`、`type=function`、`function.name` 和 JSON 字符串 `arguments`，通常
`finish_reason` 为 `tool_calls`。

失败的非流式响应由网关异常处理器包装为 `{"error": {...}}`；上游状态、错误类型和
消息会进入记账，但具体 provider 错误字段不承诺完全保留。通用错误处理见
[`exceptions.py`](../../src/rotor/core/exceptions.py) 及 Chat 路由错误分支
[`chat.py`](../../src/rotor/api/v1/chat.py)。

### 2.3 Chat SSE

响应媒体类型是 `text/event-stream`。每个数据帧为一行 `data: <JSON>`，帧之间以空行
分隔；兼容 Chat 上游收到 `[DONE]` 后停止读取并向客户端发出 `[DONE]`。Chunk 的
`object` 为 `chat.completion.chunk`，`choices[0].delta` 可含 `role`、`content` 或
增量 `tool_calls`。流式 usage 若 provider 提供，会出现在最后的 usage chunk；通用
OpenAI 适配器请求 `stream_options.include_usage=true`
([`base.py`](../../src/rotor/adapters/base.py))。

典型文本流：

```text
data: {"id":"chatcmpl_x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl_x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}

data: {"id":"chatcmpl_x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}

data: [DONE]
```

如果流在没有文本、工具调用或 provider usage 的情况下结束，当前实现抛出
`EmptyUpstreamResponse`，不会把它记录为成功的零 token 响应；实现见
[`chat.py`](../../src/rotor/api/v1/chat.py)。

## 3. OpenAI Responses

### 3.1 客户端入口和请求模型

创建入口为 `POST /v1/responses`。原生资源还包括 retrieve、delete、cancel、
input_items、input_tokens 和 compact，路径表见[客户端 API](../reference/client-api.md#openai-responses)。
模型为 [`ResponsesRequest`](../../src/rotor/schemas/responses.py)，使用
`extra="allow"` 保留未来字段。

最小请求：

```json
{
  "model": "gpt-5.6-sol",
  "input": "你好",
  "stream": false
}
```

稳定字段子集：

| 字段 | 形状 | 转换提示 |
| --- | --- | --- |
| `model` | string | 映射为 provider model |
| `input` | string 或 item[] | message、function_call、function_call_output 可转 Chat |
| `instructions` | string 或 content[] | 仅文本子集转为 Chat `system`，其他 block 丢弃 |
| `max_output_tokens`/`max_tokens` | integer | 转 Chat `max_tokens` |
| `temperature`、`top_p` | number | 基础转换保留 |
| `stream` | boolean | Responses SSE 或转换后的 Responses SSE |
| `tools` | tool[] | function 可转 Chat；hosted/namespace 语义上需 native，但当前筛选不一定强制，Chat fallback 会过滤 |
| `tool_choice` | string 或 object | function choice 可转 Chat |
| `parallel_tool_calls` | boolean | 仅原生 payload 保留 |
| `previous_response_id` | string | 需要原生 Responses，并绑定原 Channel |
| `conversation`、`background` | object/string/bool | native-only 路由能力 |
| `reasoning` | object | 当前工作树判定为 native-only；请以同版本代码为准 |
| `text`、`store`、`truncation`、`include`、`service_tier`、`user`、`max_tool_calls` | 扩展 | 未必可跨协议表达；`store` 不触发当前 native 能力筛选 |

### 3.2 非流式响应

Responses 响应模型是宽松 envelope（`extra="allow"`），见
[`ResponsesResponse`](../../src/rotor/schemas/responses.py)：

```json
{
  "id": "resp_abc",
  "object": "response",
  "created_at": 1720000000,
  "status": "completed",
  "model": "gpt-5.6-sol",
  "output": [{
    "id": "msg_1",
    "type": "message",
    "status": "completed",
    "role": "assistant",
    "content": [{"type":"output_text","text":"今天天气晴朗。","annotations":[]}]
  }],
  "usage": {
    "input_tokens": 12,
    "output_tokens": 8,
    "total_tokens": 20,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 0}
  },
  "error": null,
  "incomplete_details": null
}
```

函数输出 item 的形状为 `type=function_call`、`call_id`、`name`、字符串
`arguments`。原生 Responses 响应会保留原生 JSON 字段（SSE framing 由网关重建）；Chat/Anthropic 上游的响应会由
[`chat_response_to_responses`](../../src/rotor/adapters/protocol/responses.py)
构造上述 output item、usage 和 envelope。

失败响应通常 `status="failed"` 并含 `error={"code", "message"}`；资源请求错误见
[`responses.py`](../../src/rotor/api/v1/responses.py)。

### 3.3 Responses SSE

Responses 上游/标准 SSE 的每个事件通常包含（示例中的 `event:` 是上游 framing）：

```text
event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":4,"delta":"你好"}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_abc","status":"completed","usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6}}}
```

创建流经网关后，客户端实际收到的是 `data: <JSON>\n\n`（事件类型在 JSON 的 `type`）；
只有 retrieve 等资源 relay 才可能保留上游 framing。

解析器忽略空行、注释行（`:`）和 `event:` 行本身，只解析 `data:`。收到
`data: [DONE]` 时停止；正常 EOF 也会结束。原生 Responses 请求（`responses_payload`
存在且上游为 Responses）保留事件对象的 `type` 和 JSON 字段，但当前网关输出时统一
写成 `data: <JSON>`，不会承诺逐字节保留上游的 `event:` 行。转换路径由
`ResponsesStreamTransform`
生成事件序列：`response.created`、`response.in_progress`、output item/content
part added、text/tool delta、done，最后 `response.completed`
([`responses.py`](../../src/rotor/api/v1/responses.py))。

跨协议 Chat→Responses 的文本事件示例：

```text
data: {"type":"response.created","sequence_number":0,"response":{"object":"response","status":"in_progress","output":[]}}

data: {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"type":"message","status":"in_progress","role":"assistant","content":[]}}

data: {"type":"response.output_text.delta","sequence_number":4,"output_index":0,"delta":"你好"}

data: {"type":"response.output_text.done","sequence_number":5,"text":"你好"}

data: {"type":"response.completed","sequence_number":8,"response":{"status":"completed","output":[{"type":"message"}],"usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6}}}
```

跨协议路径没有额外 `[DONE]`；终止事件是 `response.completed`。发生异常时，网关
会生成 `response.failed`，但如果异常发生在已发送部分之后，不能保证客户端已经收到
完整事件序列。

## 4. Anthropic Messages

### 4.1 客户端入口和请求模型

入口为 `POST /anthropic/v1/messages`，计数入口为
`POST /anthropic/v1/messages/count_tokens`。Anthropic SDK 的 Base URL 应指向
`/anthropic`。请求模型为 [`AnthropicMessageRequest`](../../src/rotor/schemas/request.py)，
允许额外字段。

```json
{
  "model": "claude-test",
  "max_tokens": 256,
  "messages": [{"role":"user","content":"你好"}],
  "stream": false
}
```

字段子集：

| 字段 | 形状 | Rotor 行为 |
| --- | --- | --- |
| `model`、`messages`、`max_tokens` | 必填 | 基础字段 |
| `system` | string 或 block[] | 归一化为内部 system 前缀 |
| `temperature`、`top_p`、`top_k` | number/integer | native Anthropic 可保留；cross→Chat 不复制 `top_k` |
| `stop_sequences` | string[] | 转 Chat `stop` |
| `stream` | boolean | Anthropic SSE |
| `tools` | `{name,description,input_schema}[]` | 转 Chat function tool |
| `tool_choice` | `{type,...}` | `auto`/`any`/指定 tool 可映射 |
| `metadata` | object | 原生 payload 保留，跨协议不保证 |
| 其他 beta/供应商字段 | 扩展 | 原生 Anthropic channel 保留 |

消息 content block 支持 `text`、`image`（url 或 base64 source）、`tool_use` 和
`tool_result` 的当前转换子集。含 `role=system` 的非标准 message 会被挪到顶层
`system`，见 [`normalize_system_messages`](../../src/rotor/schemas/request.py)。

### 4.2 非流式响应

成功响应是 `type="message"` 的 Anthropic 子集：

```json
{
  "id": "msg_abc",
  "type": "message",
  "role": "assistant",
  "content": [{"type":"text","text":"今天天气晴朗。"}],
  "model": "claude-test",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 12,
    "output_tokens": 8,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

工具调用使用 `content` 中的 `tool_use` block：`id`、`name`、JSON 对象 `input`，
并以 `stop_reason="tool_use"` 结束。Chat/Responses 上游的返回由
[`openai_to_anthropic_response`](../../src/rotor/api/v1/anthropic.py)
生成；空文本会生成一个空 `text` block。

Anthropic 流失败事件的形状是 `{"type":"error","error":{"type":"stream_error","message"}}`；
非流 HTTP 错误由入口统一异常处理，可能返回 `channel_error`、`authentication_error` 等
网关分类，而不保证是 `type=error`。跨协议转换不承诺 provider 特有字段。

### 4.3 Anthropic SSE

Anthropic SSE 使用 `event: <type>` 加 `data: <JSON>`，空行分隔。原生 Anthropic channel
解析上游 `event_type` 和 JSON 后重新封装事件；当前代码识别并转发
`message_start`、`content_block_start`、
`content_block_delta`、`message_delta`、`message_stop` 以及其他未解析事件。

跨协议 Chat→Anthropic 会先发送合成 `message_start`，随后文本和工具 block 事件，流
结束时发送 block stop、`message_delta`（含 `stop_reason`/usage）和 `message_stop`。
`OpenAIToAnthropicStreamConverter` 会等到上游流结束，确保尾随 usage chunk 已看到
后再发送终止事件，见 [`anthropic.py`](../../src/rotor/api/v1/anthropic.py)。

Anthropic 路径不使用 `[DONE]` 作为正常终止标记；`message_stop` 只保证由转换流合成的
正常终止。native 上游若以 EOF 或 `[DONE]` 结束且未带 `message_stop`，适配器不会补发。
若发生异常，入口发送 `event: error`，错误对象类型为 `stream_error`，而不是伪造一
个成功的 `message_stop`。

## 5. 字段级转换矩阵

下表是“Rotor 当前实现的可表达子集”，不是协议完整互操作保证。空白表示没有直接映射，或只在 native payload 中保留。

### 5.1 Chat ↔ Responses

| Chat 字段/语义 | Chat→Responses | Responses→Chat | 备注 |
| --- | --- | --- | --- |
| `model` | `model` | `model` | 最终再做 Channel `model_mapping` |
| system message | `message.role=developer` | `instructions`→system；developer→system | Responses 使用 developer 角色 |
| user/assistant text | 字符串保留字符串；块数组中的 text→`input_text` | `type=message` content→字符串/块 | 仅识别 text/input_text/output_text/refusal 与输入图片 |
| `image_url` | `input_image.image_url`，保留 `detail` | `input_image` url→`image_url` | file_id 等引用不转 Chat |
| assistant `tool_calls` | 独立 `function_call` item | 合并为 assistant `tool_calls` | arguments 保持字符串 |
| tool message | `function_call_output` | `role=tool`，output 保留字符串或内容值 | `call_id` 保留；Responses→Chat 会对非字符串值 JSON 字符串化 |
| function tool | `{type:function,name,description,parameters,strict}` | `Tool(function=...)` | hosted/namespace 语义上需 native；当前筛选不一定强制，Chat fallback 会过滤 |
| function tool choice | `{type:function,name}` | `{type:function,function:{name}}` | required/hosted 可能要求 native |
| `temperature`,`top_p`,`user` | 同名 | 同名 | `user` 仅 Chat→Responses 保留；基础字段可转换 |
| `max_tokens` | `max_output_tokens` | `max_output_tokens`→`max_tokens` | alias 兼容 |
| usage input/output | `input_tokens`/`output_tokens` | `prompt_tokens`/`completion_tokens` | details 放在对应 details 对象 |
| `n`, penalties, stop | 无直接 Responses 字段 | 不生成 | 有损，不应依赖；转换只取 `choices[0]` |
| response text | output message + `output_text` | `message.content` 拼接 | 仅读取 text/input_text/output_text/refusal；其他 output block 忽略，多 item 文本拼接 |
| response function call | output function_call item | `tool_calls[]` | 缺 ID 时生成随机 ID |

转换实现是 [`chat_request_to_responses_payload`](../../src/rotor/adapters/protocol/responses.py)、
[`responses_request_to_chat`](../../src/rotor/adapters/protocol/responses.py)、
[`responses_response_to_chat`](../../src/rotor/adapters/protocol/responses.py) 和
[`chat_response_to_responses`](../../src/rotor/adapters/protocol/responses.py)。

Chat→Responses 请求样例：

```json
{
  "model":"gpt-5.6-sol",
  "input":[
    {"type":"message","role":"developer","content":"你是天气助手"},
    {"type":"message","role":"user","content":"上海天气？"},
    {"type":"function_call_output","call_id":"call_1","output":"{\"temperature\":30}"}
  ],
  "stream":false,
  "max_output_tokens":256,
  "tools":[{"type":"function","name":"get_weather","parameters":{"type":"object","properties":{}}}]
}
```

Responses→Chat 响应样例：

```json
{
  "id":"resp_1",
  "object":"chat.completion",
  "created":1720000000,
  "model":"gpt-5.6-sol",
  "choices":[{"index":0,"message":{"role":"assistant","content":"晴朗"},"finish_reason":"stop"}],
  "usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5,"prompt_tokens_details":{},"completion_tokens_details":{}}
}
```

### 5.2 Chat ↔ Anthropic

| Chat 字段/语义 | Chat→Anthropic | Anthropic→Chat | 备注 |
| --- | --- | --- | --- |
| system | 顶层 `system` 文本 | 顶层 system→`role=system` | 多段 system 用换行连接 |
| user/assistant text | content text block | text block→字符串 | 仅 Chat→Anthropic 会合并相邻相同角色 |
| `image_url` | image source url 或 base64 | image source→`image_url` | 仅支持可解析 url/base64 |
| assistant `tool_calls` | `tool_use` blocks，arguments JSON→object | tool_use→Chat `tool_calls`，input JSON→字符串 | ID/name 保留 |
| tool message | user 的 `tool_result` block | tool_result→`role=tool` | `is_error` 不进入 Chat 字段 |
| function tool | `name`,`description`,`input_schema` | `Tool.function.parameters` | strict 无 Anthropic 对应 |
| tool choice auto/required/name | auto/any/tool | auto/required/function | none 不发送字段 |
| `max_tokens` | `max_tokens`，缺省 4096 | `max_tokens` | Anthropic 请求必填 |
| `temperature`,`top_p`,`stop` | 同名/`stop_sequences` | 同名/`stop` | top_k、metadata 无 Chat 字段；cross→Chat 不复制 top_k |
| Chat usage | prompt details→cache 字段 | input/cache 字段→prompt details | 未知桶默认为 0 |
| finish reason | — | end_turn→stop；max_tokens→length；tool_use→tool_calls | 映射表固定 |

实现见 [`ProtocolConverter`](../../src/rotor/adapters/protocol/converter.py)、
[`anthropic_to_openai_request`](../../src/rotor/api/v1/anthropic.py) 和
[`openai_to_anthropic_response`](../../src/rotor/api/v1/anthropic.py)。

Anthropic→Chat 遇到同一 user 消息混合文本与图片时，当前实现会拆成多个 user 消息；
Anthropic system 的 billing/cache 扩展只在特定 Responses GPT-5.6 桥接中使用，普通
Chat cross 路径会过滤。

Chat→Anthropic 请求样例：

```json
{
  "model":"claude-test",
  "messages":[
    {"role":"user","content":[{"type":"text","text":"上海天气？"}]},
    {"role":"assistant","content":[{"type":"tool_use","id":"call_1","name":"get_weather","input":{"city":"Shanghai"}}]},
    {"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":"{\"temperature\":30}"}]}
  ],
  "max_tokens":4096,
  "tools":[{"name":"get_weather","description":"查询天气","input_schema":{"type":"object","properties":{"city":{"type":"string"}}}}],
  "stream":false
}
```

Anthropic→Chat 响应样例：

```json
{
  "id":"chatcmpl-随机",
  "object":"chat.completion",
  "created":1720000000,
  "model":"claude-test",
  "choices":[{"index":0,"message":{"role":"assistant","content":"晴朗"},"finish_reason":"stop"}],
  "usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20,"prompt_tokens_details":{"cached_tokens":0,"cache_write_tokens":0,"cache_write_5m_tokens":0,"cache_write_1h_tokens":0,"uncached_tokens":12}}
}
```

### 5.3 Responses ↔ Anthropic：经内部 Chat 规范化

Rotor 没有独立的 Responses↔Anthropic 直接转换器。Responses 入口先调用
`responses_request_to_chat`，Anthropic channel 再按 Chat→Anthropic 转换；Anthropic
入口先调用 `anthropic_to_openai_request`，Responses channel 再按 Chat→Responses
转换。因此：

```text
Responses 客户端 → ResponsesRequest → ChatCompletionRequest → Anthropic payload
Anthropic 客户端 → AnthropicMessageRequest → ChatCompletionRequest → Responses payload
```

这条路径会丢弃或降级不能放入 Chat 的字段（可选 hosted/namespace tool、以及 Anthropic
扩展 block 等）。可检测的 Responses native-only 字段（conversation 状态、background、
reasoning、非 message item、不可转换 block 或不可表达的 tool choice）会被
`responses_required_capabilities` 标为 `responses_native`，路由会排除 Chat/Anthropic
候选；没有 native Responses Channel 时返回无兼容 Channel，而不是静默跨协议丢弃。
`store` 不参与当前 native 判定，因而在允许 cross 时仍可能丢失。原始 Responses body 和
Anthropic body 各自仍可在对应 native channel 使用，见下一节。

例外是当前工作树对 Anthropic system `cache_control` 的有限桥接：选到支持的 GPT-5.6
Responses model 时，最多保留最近四个显式 breakpoint，并可附 `prompt_cache_key` 与
30m implicit TTL。若该上游以 400/`invalid_parameter` 表示不支持
`prompt_cache_breakpoint`，Responses adapter 会在同一 Channel 深拷贝请求并递归移除
这些 breakpoint 后重试一次；`prompt_cache_options`/key 等其余提示仍可保留。其他模型
或目标协议不保证保留。

## 6. Payload 保留与原生能力边界

### 6.1 `responses_payload`

`responses_request_to_chat` 将 `request.provider_payload()` 放到内部请求的
`responses_payload`；它包含 Responses 模型允许的额外字段。若目标适配器是原生
Responses，`OpenAIResponsesAdapter.convert_request` 深拷贝它，只覆盖映射后的
`model` 和 `stream`，并按模型条件处理 prompt cache 字段。若 provider 明确拒绝
`prompt_cache_retention` 或 `prompt_cache_breakpoint`，`make_request` 会在同一 Channel
对深拷贝请求做一次对应字段剥离重试（每类最多一次；两类同时被拒时最多两次），这不是
通用同渠道重试。若目标是 Chat 或 Anthropic，转换器只使用归一化 messages/tools，不把
原始 payload 泄漏到另一协议。

原生 Responses 非流式响应和 SSE 事件对象在
[`OpenAIResponsesAdapter.convert_response`](../../src/rotor/adapters/protocol/responses.py)
及 [`stream_convert_response`](../../src/rotor/adapters/protocol/responses.py)
中按 JSON event object 保留；创建流的客户端 framing 由网关重建为 `data:` 行。只有
retrieve 等资源 relay 路径才可能逐字节转发上游流。

### 6.2 `anthropic_payload` 与 headers

`anthropic_to_openai_request` 将 `model_dump(exclude_none=True)` 保存到
`anthropic_payload`，并把请求中的 `anthropic-beta`/`anthropic-version` 转为
`anthropic_headers`。原生 Anthropic adapter 深拷贝 payload，从而保留 `cache_control`、
结构化 system、tool-result error state 和未来扩展；跨协议目标通常不会使用这些字段，唯一
当前例外是上一节所述的 Anthropic `cache_control`→GPT-5.6 Responses breakpoint 有限桥接。
实现见 [`AnthropicAdapter.convert_request`](../../src/rotor/adapters/protocol/anthropic.py)。

### 6.3 Responses native-only 能力

以下字段/操作需要 `Channel.protocol` 为 Responses，并且通常要求回到创建 response
的同一上游账号：

- `previous_response_id`（路由会检查本 Token 的 response route，失败时不跨 provider
  fallback）；
- `conversation`、`background` 等状态或后台语义（`store` 没有跨协议等价字段，但当前
  `responses_required_capabilities` 不因 `store` 单独筛选 native）；
- `reasoning` 选项在当前工作树会触发 `responses_native`；这是代码实现，不代表旧 HEAD
  或所有部署版本都相同；
- hosted tools、namespace tools、非 function tool 在语义上需要 native，但当前
  `responses_required_capabilities` 并未一律强制；hosted-only 请求可能先过滤工具后
  走 Chat，存在语义丢失风险（测试覆盖可选工具 fallback）；
- reasoning item、加密内容、非 message input item；
- retrieve、delete、cancel、input_items、input_tokens、compact 资源操作；
- 依赖 provider response ID、服务端状态、异步完成或原生事件序列的客户端逻辑。

Responses 路由会用 `responses_required_capabilities` 标记
`responses_native`，并优先 native candidate；函数工具等基础请求仍可使用转换候选。
实现见 [`responses_required_capabilities`](../../src/rotor/adapters/protocol/responses.py)
和 [`create_response`](../../src/rotor/api/v1/responses.py)。

### 6.4 不可无损转换的字段

下列语义没有稳定的 Chat 对应：Responses 的 `previous_response_id`、conversation
状态、background、reasoning、hosted/namespace tools、非 message item、资源 ID；
Anthropic 的 `thinking`/签名 block、cache_control 精细结构、beta 扩展、tool_result
`is_error` 和部分 metadata。它们可能在 native 路径保留，但 cross-protocol 只能过滤、
降级、合成占位内容或丢弃。不要把“字段被 `extra=allow` 接受”误认为“字段已经跨协议
实现”。

## 7. 三种流式事件转换表

### 7.1 Chat 上游 → Chat 客户端

| 上游输入 | Rotor 输出 |
| --- | --- |
| `data: {Chat chunk}` | 原样 JSON chunk |
| `data: [DONE]` | 停止读取并输出 `[DONE]` |
| usage-only chunk | 记录 usage；通常随最后 chunk 输出 |
| 中途 `error` 对象 | overload/rate-limit 触发异常；其他 error 可继续转发 |
| 无内容/工具/usage 的 EOF | `EmptyUpstreamResponse`，进入失败路径 |

### 7.2 Responses 上游 → Responses 客户端或 Chat 客户端

| Responses 事件 | native Responses 客户端 | Chat 客户端（cross） |
| --- | --- | --- |
| `response.created`、`response.in_progress` | 语义 JSON 字段保留；SSE framing 重建 | 不产生 Chat chunk（由 Chat 内容开始时生成） |
| `response.output_text.delta` | 语义 JSON 字段保留；SSE framing 重建 | `choices[0].delta.content` |
| `response.output_item.added` function_call | 语义 JSON 字段保留；SSE framing 重建 | 首个 tool delta，含 id/name |
| `response.function_call_arguments.delta` | 语义 JSON 字段保留；SSE framing 重建 | `tool_calls[].function.arguments` |
| `response.output_item.done` | 语义 JSON 字段保留；SSE framing 重建 | 必要时补完整 arguments |
| `response.completed` | 语义 JSON 字段和 native usage 保留；SSE framing 重建 | 空 delta + finish_reason + usage |
| `error` / `response.failed` | 适配器解析为异常，不当作成功事件；网关可能生成新的失败事件 | 同上；网关随后可生成失败响应 |
| `[DONE]` | 若上游提供则停止消费；网关不保证为 native Responses 生成 | 停止读取 |

转换代码只识别上表事件；其他 Responses 事件在 cross 路径不会自动映射。

### 7.3 Anthropic 上游 → Anthropic 客户端或 Chat 客户端

| Anthropic 事件 | native Anthropic 客户端 | Chat 客户端（cross） |
| --- | --- | --- |
| `message_start` | 语义 JSON 字段保留并重封装，观察 input usage | 初始 assistant role chunk，并映射 usage |
| `content_block_start` text | 语义 JSON 字段保留并重封装 | 无直接输出，文本首个 delta 时体现 |
| `content_block_start` tool_use | 语义 JSON 字段保留并重封装 | tool_calls 首 chunk，含 id/name |
| `content_block_delta.text_delta` | 语义 JSON 字段保留并重封装 | `delta.content` |
| `content_block_delta.input_json_delta` | 语义 JSON 字段保留并重封装 | `tool_calls[].function.arguments` |
| `thinking_delta` | 语义 JSON 字段保留并重封装 | 当前转换器把 thinking 文本作为 content（供应商扩展行为） |
| `message_delta` | 语义 JSON 字段保留并重封装 | finish_reason/usage，映射 stop_reason |
| `message_stop` | 语义 JSON 字段保留并重封装 | 不产生额外 Chat chunk |
| `error` | 适配器转为异常，入口生成 `event:error` 的 `stream_error` | overload 抛 `UpstreamOverloaded`，其他错误抛 RuntimeError |

Chat/OpenAI 上游 → Anthropic 客户端则由
[`OpenAIToAnthropicStreamConverter`](../../src/rotor/api/v1/anthropic.py)
反向生成 `content_block_*`、`message_delta` 和 `message_stop`；它不是 provider 原始
事件的字节级转发。

## 8. Chat→Responses 精确调用链

下面以 `POST /v1/chat/completions` 请求选择 `protocol=openai_responses` 的 Channel
为例，列出实际顺序。

1. **路由入口**：FastAPI 调用 `chat_completions`，解析 `ChatCompletionRequest`，
   固定 `request_protocol="openai_chat"`，解析 Token、session、模型候选。
2. **Factory**：每次 attempt 调用 `AdapterFactory.create_adapter(channel, http_client)`。
   当 `channel.protocol` 为 `responses`/`openai_responses` 时，无论 `channel.type` 是
   什么都选择 `OpenAIResponsesAdapter`
   ([`factory.py`](../../src/rotor/adapters/factory.py))。
3. **URL、headers、model**：适配器使用 `extra.request_path` 或 `/responses`，通过
   `join_api_url` 拼接 `base_url`；`provider_headers` 生成 Bearer（或显式 auth_type），
   `model_mapping[logical_model]` 得到 provider model。流式请求加
   `Accept: text/event-stream`。
4. **请求转换**：`OpenAIResponsesAdapter.convert_request` 调用
   `chat_request_to_responses_payload`，把 system→developer、消息→message item、
   tool call→function_call、tool→function_call_output，并映射 tools/choice/max tokens。
   实现见 [`responses.py`](../../src/rotor/adapters/protocol/responses.py)。
5. **上游调用**：`BaseAdapter.make_request` POST 配置的 `request_path`（默认
   `/responses`）；非流式读取 JSON，流式保持 response stream。
6. **非流式响应**：Responses adapter 的 `convert_response` 看到请求没有
   `responses_payload`，调用 `responses_response_to_chat`；Chat 路由将 Chat JSON 返回
   给客户端并按 `request_protocol=openai_chat` 记账。
7. **流式响应**：adapter 的 `stream_convert_response` 将
   `response.output_text.delta`、function call delta、`response.completed` 映射成
   Chat chunks；`_handle_streaming_request` 先生成并暂存终止事件，再同步写入 usage、
   attempt 和 lease，并将 ConversationStore 响应归档入异步队列，最后才逐帧发送终止
   `data: [DONE]`。原始 Responses 逻辑行会先记录 INFO，
   但日志不是协议保证。
8. **失败与 fallback**：建立连接或 HTTP 错误在 attempt 循环中按
   `should_fallback` 决定是否标记 Channel 不可用并尝试下一个；流已开始后，异常只走
   流失败包装，不会透明重放已发送内容。Responses 资源型状态请求则必须返回原生
   Channel，不能靠跨 provider fallback 维持状态链。

对照源码：入口和 attempt 在 [`chat.py`](../../src/rotor/api/v1/chat.py)，
非流式/流式公共处理在 [`chat.py`](../../src/rotor/api/v1/chat.py) 与
[`chat.py`](../../src/rotor/api/v1/chat.py)。

## 9. 错误、终止、fallback 与原始 SSE 日志

### 9.1 三种协议的终止标记

| 客户端协议 | 正常终止 | `[DONE]` |
| --- | --- | --- |
| Chat | Chat chunk 的 `finish_reason`，随后 `[DONE]` | 使用 |
| Responses | `response.completed`（或 native 的其他 terminal event） | 解析器接受并停止；cross transform 不额外发 |
| Anthropic（转换流） | `message_delta` 后 `message_stop` | 不使用；native EOF/[DONE] 不自动补发 |

Native Responses 还可能出现 `response.failed`、`response.cancelled`、
`response.incomplete`；网关会延迟 terminal event，先完成 usage 记账再发出，避免
客户端在 SQLite 清理前断开，见 [`chat.py`](../../src/rotor/api/v1/chat.py)。

### 9.2 `Responses upstream stream failed` 来源

`OpenAIResponsesAdapter._stream_error_details` 在 Responses `error` 或
`response.failed` 事件没有可读 message 时使用固定文本 `Responses upstream stream failed`
([`responses.py`](../../src/rotor/adapters/protocol/responses.py))。它还会读取
顶层或嵌套的 `code`/`type`，overload 信号会抛出 `UpstreamOverloaded`，其他错误抛
带 `error_type` 属性的 `RuntimeError`。这不是 provider 原始错误的规范化保证。
向上层格式化时，`format_error_message` 可能显示为 `RuntimeError: Responses upstream stream failed`。

### 9.2.1 错误 envelope 最小形状

错误对象是当前入口的最小可互操作字段；供应商扩展字段可能被记录但不保证回传。

```json
{"error":{"message":"上游拒绝请求","type":"invalid_request_error","code":"invalid_parameter"}}
```

Chat 流中则发送：

```text
data: {"error":{"message":"Upstream stream error","type":"stream_error"}}
```

其中 `message` 仅为示例值，实际内容取决于上游异常。

Responses 流失败事件：

```text
data: {"type":"response.failed","sequence_number":0,"response":{"status":"failed","error":{"code":"RuntimeError","message":"Responses upstream stream failed"}}}
```

Anthropic 流失败事件：

```text
event: error
data: {"type":"error","error":{"type":"stream_error","message":"upstream failed"}}
```

三者都至少包含可读 `message`；Responses 还可含 `code`。Messages 流异常由网关固定为
`error.type="stream_error"`，provider 的原始类型/消息用于日志与记账，不保证原样回传。
错误事件不是成功终止事件，客户端应停止累积并按入口协议处理。

### 9.3 fallback 发生时点

以下候选循环说明适用于消息创建路径（Chat、Responses create、Anthropic Messages）。
普通候选循环每个 Channel 尝试一次。Responses adapter 另有两类有界兼容重试：上游以
400/`invalid_parameter` 拒绝 `prompt_cache_retention` 时删除该字段重试一次；确认
`prompt_cache_breakpoint` 不受支持时递归删除该字段重试一次。两类字段若同时被拒，
单次 `make_request` 最多进行两次这种同 Channel 变体重试；这不是通用同渠道重试循环。

- **HTTP 建立前/非流式响应阶段**：`HTTPStatusError` 或 `RequestError`（包括 adapter
  兼容重试耗尽后传播出的最终错误）经 `should_fallback` 判定后，Router 可标记当前
  Channel 冷却并尝试下一候选。传播到入口层的失败会写入 RequestLog/Attempt；被 adapter
  内部吸收的兼容错误不会单独生成候选 Attempt。
- **流式上游已打开但尚未向客户端发完**：适配器异常进入流生成器失败分支；会发送
  当前客户端协议的错误事件（Chat `error`、Responses `response.failed`、Anthropic
  `event:error`），不会重放已经发出的文本或工具副作用。
- **流式已向客户端发出部分内容后**：不再换 Channel；客户端应把错误视为本次流失败。

具体 fallback 判定由 [`fallback.py`](../../src/rotor/gateway/fallback.py) 的
`should_fallback` 决定，不能仅凭 HTTP 状态码推断所有结果。

### 9.4 Responses 原始逐行日志范围

Responses adapter 的 `_logged_sse_lines` 在解析前以 INFO 记录 `aiter_lines()` 产出的每一条逻辑行的 `repr`
（不是原始字节行或抓包记录），包括注释、`event:`、空行、`data:`、无效 JSON
和 `[DONE]`；收到 `[DONE]` 后停止消费，因此后续行不会记录；并记录 start、正常 EOF
（`reason=eof`）或迭代异常。日志包含 request/channel/model/provider_model/
request_protocol/provider_protocol 上下文，测试见
[`test_responses_stream_logs_each_raw_sse_line_at_info`](../../tests/test_responses_protocol.py)。

该日志只覆盖 `OpenAIResponsesAdapter.stream_convert_response` 创建流的路径：Chat→Responses、
Responses 原生 create，以及 Anthropic 入站请求选到 Responses Channel 的转换流；不覆盖
Responses retrieve 的 relay、Images、普通 Chat adapter 或 Anthropic adapter。INFO 日志可能
含 prompt、tool 参数或加密内容等敏感数据，应按磁盘权限、保留周期和脱敏策略评估风险。

启用 INFO 文件日志时，`MonthlyDailyFileHandler` 对每条记录执行 flush，按本地时区写入 `ROTOR_LOG_DIR/YYYY-MM/YYYY-MM-DD.log`；未启用 INFO 时这些逐行记录不会落盘。flush 提高
取证时效，但不等同于崩溃级持久化或完整审计保证。

这只是当前诊断实现：日志级别、采样、保留周期、敏感字段脱敏和完整性都未形成协议
保证；不要把“已记录原始行”解释为通用抓包或合规审计存档。迭代断开时对应的
`Responses SSE exception` 由 [`responses.py`](../../src/rotor/adapters/protocol/responses.py)
记录，测试见 [`test_responses_stream_logs_upstream_iteration_exception`](../../tests/test_responses_protocol.py)。

解析器按单行 `data:` JSON 处理，不保证把多行 SSE data 合并；因此“完整”仅指已消费的
逻辑行诊断，不是原始字节或完整事件抓包。

若需要三协议和资源流的统一审计，仍需在 Chat/Anthropic adapter 及 Responses retrieve
relay 增加同类记录；当前实现不是全链路完整日志。

## 源码与测试索引

### Schema 与适配器

- [Chat/Anthropic schema](../../src/rotor/schemas/request.py)
- [Responses schema](../../src/rotor/schemas/responses.py)
- [Responses adapter 与转换器](../../src/rotor/adapters/protocol/responses.py)
- [Chat↔Anthropic converter](../../src/rotor/adapters/protocol/converter.py)
- [Anthropic adapter](../../src/rotor/adapters/protocol/anthropic.py)
- [Adapter Factory](../../src/rotor/adapters/factory.py)
- [Base adapter 与 headers/URL](../../src/rotor/adapters/base.py)
- [Provider presets](../../src/rotor/channels/presets.py)

### API 路由

- [Chat route](../../src/rotor/api/v1/chat.py)
- [Responses route](../../src/rotor/api/v1/responses.py)
- [Anthropic route](../../src/rotor/api/v1/anthropic.py)
- [客户端 API 参考](../reference/client-api.md)
- [Channel 字段参考](../reference/channel-schema.md)
- [整体设计快照](../../docs/rotor-design.md)

### 关键测试

- [Responses protocol tests](../../tests/test_responses_protocol.py)
- [Anthropic protocol tests](../../tests/test_anthropic_protocol.py)
- [Channel preset/URL/header tests](../../tests/test_channel_presets.py)
- [OpenAI SDK compatibility tests](../../tests/test_openai_sdk.py)
- [Client session/header tests](../../tests/test_client_session.py)
- [Chat/Responses/Anthropic API tests](../../tests)

## 限制与维护提示

本文只记录当前实现，不承诺完整官方协议、跨 provider 字节等价、所有 usage 细分、未来字段自动兼容或失败后的 exactly-once 语义。新增字段、事件或 adapter 时，应先更新 schema/转换器和测试，再同步本手册的矩阵、样例与限制；若无法表达，应明确标为 native-only，而不是静默声称已转换。
