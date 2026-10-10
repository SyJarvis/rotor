# 客户端 API

本页列出 Rotor 面向模型客户端的稳定 HTTP 入口。请求体以对应供应商协议为基础；
Rotor 支持范围和原生能力差异见[协议兼容与转换](../concepts/protocols.md)。

下文“推理凭证”指 Rotor API Key 或[客户端 OAuth access token](../guides/client-oauth.md)。
Bearer 支持两者，`x-api-key` 仅支持 API Key；授权码和 refresh token 不能推理或访问
管理 API。网页对话另使用账户 Session Cookie，直接按账户权限和额度执行，不依赖
基础或默认 Key。

## 服务信息

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/health` | 无 | 进程健康与协议列表 |
| `GET` | `/api` | 无 | 服务元数据和主要入口 |
| `GET` | `/v1/models` | 可选 | 无凭证列出启用渠道模型及映射键；有效推理凭证应用渠道和模型权限 |

## OpenAI Chat 和 Images

| 方法 | 路径 | 认证 |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | 推理凭证 |
| `POST` | `/v1/images/generations` | 推理凭证 |

Chat 支持非流式和 SSE 流式响应。Images 请求保留扩展字段，并使用 Channel 的
模型映射和专用 images 路径。

## OpenAI Embeddings

`POST /v1/embeddings` 使用推理凭证，支持文本、文本数组、Token ID 数组和
Token ID 数组的数组；支持 `dimensions`、`encoding_format`（`float` / `base64`）、
`user` 和供应商扩展字段。响应原样返回，usage 计入所属账户配额；API Key 请求另计入
Key 配额，无所属账户的旧 Key 只计入其自身配额。

渠道需使用 OpenAI 兼容协议（包括配置为 Responses 的渠道），并在 `models` 或
`model_mapping` 中配置 embedding 模型。不支持转换为 Anthropic Messages。
如果设置了 `extra.capabilities`，须包含 `embeddings`。
默认上游路径为 `/embeddings`，可使用 `extra.embeddings_path` 覆盖；它独立于
Chat / Responses 的 `request_path`，与渠道的 `base_url` 拼接。

```bash
curl http://localhost:8000/v1/embeddings \
  -H "Authorization: Bearer $ROTOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"text-embedding-3-small","input":["你好","Rotor"],"encoding_format":"float"}'
```

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

所有接口都需要推理凭证。资源生命周期、后台状态、hosted tools 和
`previous_response_id` 需要原生 Responses 渠道。Rotor 保存 response ID 的上游归属：
API Key 请求按 Key ID 隔离，OAuth 请求按账户 ID 隔离。同账户的 OAuth 授权共享
账户范围，但不能访问 API Key 范围中的资源。ChatGPT OAuth 上游渠道的持久资源
限制见[渠道登录说明](../guides/chatgpt-oauth.md#授权检查与请求限制)。

## Anthropic Messages

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/anthropic/v1/messages` | 创建 Messages 响应 |
| `POST` | `/anthropic/v1/messages/count_tokens` | 计算输入 Token |

Bearer 可携带 API Key 或 OAuth access token，`x-api-key` 仅用于 API Key。
`GET /anthropic/v1/models` 也需要推理凭证，并按实际渠道、模型权限过滤。
Anthropic SDK 的 Base URL 应设置为
`http://host:port/anthropic`，由 SDK 追加 `/v1/messages`。

## 通用请求头

| Header | 是否必需 | 用途 |
| --- | --- | --- |
| `Authorization: Bearer ...` | 与 `x-api-key` 二选一 | Rotor API Key 或 OAuth access token |
| `x-api-key` | 与 Bearer 二选一 | Rotor API Key；不接受 OAuth 凭证 |
| `X-Conversation-Id` | 否 | 会话亲和、usage 关联和会话存储 |
| `X-Rotor-Session-Id` | 否 | 与 `X-Conversation-Id` 等价的显式 Rotor Session ID |
| `Content-Type: application/json` | JSON 请求需要 | 请求体类型 |

Rotor 也会识别 Codex 的 `session-id`、Claude Code 的
`x-claude-code-session-id`，以及 OpenCode 的 `x-session-affinity` / `x-session-id`。
显式 `X-Conversation-Id` 优先级最高。没有稳定 Session 信号时，请求仍可执行，但不启用
会话亲和；Rotor 为会话归档生成的临时 ID 不参与路由。
