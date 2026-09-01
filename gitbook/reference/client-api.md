# 客户端 API

本页列出 Rotor 面向模型客户端的稳定 HTTP 入口。请求体以对应供应商协议为基础；
Rotor 支持范围和原生能力差异见[协议兼容与转换](../concepts/protocols.md)。

## 服务信息

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/health` | 无 | 进程健康与协议列表 |
| `GET` | `/api` | 无 | 服务元数据和主要入口 |
| `GET` | `/v1/models` | 可选 | 启用渠道公开的模型；有效 Token 会应用渠道限制 |

## OpenAI Chat 和 Images

| 方法 | 路径 | 认证 |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Rotor Token |
| `POST` | `/v1/images/generations` | Rotor Token |

Chat 支持非流式和 SSE 流式响应。Images 请求保留扩展字段，并使用 Channel 的
模型映射和专用 images 路径。

## OpenAI Responses

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/responses` | 创建 response |
| `GET` | `/v1/responses/{response_id}` | 检索原生 response |
| `DELETE` | `/v1/responses/{response_id}` | 删除原生 response |
| `POST` | `/v1/responses/{response_id}/cancel` | 取消原生 response |
| `GET` | `/v1/responses/{response_id}/input_items` | 列出输入项 |
| `POST` | `/v1/responses/input_tokens` | 计算输入 Token |
| `POST` | `/v1/responses/compact` | 请求 compaction |

所有接口都需要 Rotor Token。资源生命周期、后台状态、hosted tools 和
`previous_response_id` 需要原生 Responses 渠道。Rotor 以 Token 为范围保存
response ID 的上游归属。

## Anthropic Messages

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/anthropic/v1/messages` | 创建 Messages 响应 |
| `POST` | `/anthropic/v1/messages/count_tokens` | 计算输入 Token |

可以使用 Bearer Token 或 `x-api-key`。Anthropic SDK 的 Base URL 应设置为
`http://host:port/anthropic`，由 SDK 追加 `/v1/messages`。

## 通用请求头

| Header | 是否必需 | 用途 |
| --- | --- | --- |
| `Authorization: Bearer ...` | 与 `x-api-key` 二选一 | Rotor Token |
| `x-api-key` | 与 Bearer 二选一 | Anthropic 风格认证 |
| `X-Conversation-Id` | 否 | 会话亲和、usage 关联和会话存储 |
| `X-Rotor-Session-Id` | 否 | 与 `X-Conversation-Id` 等价的显式 Rotor Session ID |
| `Content-Type: application/json` | JSON 请求需要 | 请求体类型 |

Rotor 也会识别 Codex 的 `session-id`、Claude Code 的
`x-claude-code-session-id`，以及 OpenCode 的 `x-session-affinity` / `x-session-id`。
显式 `X-Conversation-Id` 优先级最高。没有稳定 Session 信号时，请求仍可执行，但不启用
会话亲和；Rotor 为会话归档生成的临时 ID 不参与路由。
