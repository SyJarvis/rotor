# 协议兼容与转换

Rotor 将客户端请求统一路由到 Channel，但 Channel 可以使用与客户端不同的上游
协议。

三种消息协议的请求/响应/SSE 字段和转换矩阵见[详细格式手册](protocol-formats.md)。

## 协议入口

| 客户端入口 | 原生或可转换的上游 |
| --- | --- |
| Chat Completions | OpenAI Chat、Anthropic、原生 Responses |
| Responses | 原生 Responses；基础请求也可转换到 Chat/Anthropic |
| Anthropic Messages | 原生 Anthropic；基础请求也可转换到 Chat/Responses |
| Images | 专用上游 Images 路径 |

## 原生 Responses

`protocol=openai_responses` 表示上游原生实现 Responses API。Rotor 会保留原生
Responses 事件对象的语义字段，并由网关重建客户端 SSE framing（不是字节级透传）；同时
持久化 response ID 到渠道和 Rotor Token 的绑定。检索、取消、删除、
输入项列表、input token 计数、compaction 和 `previous_response_id` 会回到原始
上游账号。

依赖 hosted tools、后台任务、状态化 response 或资源生命周期操作时，必须使用
原生 Responses 渠道。仅使用文本、流式输出或函数工具的基础请求可以在兼容渠道
间转换。

## 原生 Anthropic

`protocol=anthropic` 使用 Anthropic 的 `/messages` 请求、`x-api-key` 鉴权和
事件格式。原生路径会保留支持的 Anthropic 扩展字段和 beta header；SSE 事件会按
`event_type`/JSON 解析后重新封装，非字节级透传。跨协议转换
覆盖文本、system 内容、工具定义、工具调用和标准流式事件，但不承诺保留所有
供应商扩展。

## Images

Images 请求使用独立的 `/images/generations` 上游路径，不通过 Chat 或 Responses
请求路径。即使 Channel 的协议是 `openai_responses`，Images 入口仍使用专用路径。
非标准供应商可以通过 Channel 的 `extra.images_path` 覆盖。

精确端点见[客户端 API](../reference/client-api.md)。
