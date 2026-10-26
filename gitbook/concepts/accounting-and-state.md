# 记账、会话与状态

Rotor 将请求审计、账户与 API Key 配额、会话正文和原生 Responses 归属分成不同的数据
结构，避免把它们混成单一日志。

## 请求日志和用量账本

`request_logs` 面向管理页查询，记录模型、渠道、成功状态、延迟、错误和基本用量。
`usage_ledger` 是按请求追加的用量记录，包含客户端/上游协议、映射后的模型、
细分 Token、用量来源和状态。

成功的网页对话、OAuth 请求和账户所属 API Key 请求共同增加账户的 `used_quota`，
每次用量只累计一次。API Key 请求另增加该 Key 的请求数、Token 数和已用配额；
无所属账户的旧 Key 只增加自身计数。上游未提供 usage 时，`usage_source` 为 `missing`，
不会用估算值冒充供应商数据。

`request_logs` 和 `usage_ledger` 中，网页对话与 OAuth 请求记录 `owner_id`，
`token_id` 为 `null`；账户所属 API Key 请求记录两者，无所属账户的旧 Key 只有
`token_id`。`owner_id` 是账户归属，`user_id` 是兼容的客户端标识，不能用后者替代前者。

缓存读、缓存写和未缓存输入按 provider 的原始语义归一后分别保存。显式配置
Channel tariff 时，账本还会保存成功上游 attempt 开始时生效的费率版本、时段、
快照和计算费用；没有 usage 或 tariff 时费用保持 unknown，不把缺失事实当作免费。
账本还保存请求发生时生效的 cache、capacity 和 billing scope，避免 Channel 配置
变化后重新解释历史资源边界。

## 路由决策

`routing_decisions` 保存每次生成的候选渠道、选中渠道、策略版本、能力要求、评分和
特征快照，其中包括候选 Channel 当时的三个资源 scope。它通过 `request_id` 与 usage
ledger 关联。

## Session Lease

`session_leases` 是多 worker 共享的活跃会话路由状态，键为认证范围、Session ID
和 logical model：API Key 使用 Key ID，网页对话与 OAuth 使用账户 ID。
同账户多个 OAuth 授权共享账号范围，账号与 Key 范围彼此隔离。租约及其事件中
`token_id`、`owner_id` 必须且只能有一个非空。`session_lease_events` 是追加式转换事实，
通过 request ID 与路由、attempt 和 usage 关联。租约不包含 prompt 或 provider KV Cache，
也不替代客户端上下文。

## 会话存储

设置 `X-Conversation-Id` 后，请求生命周期事件可由 ConversationStore 异步写入
`CONVERSATION_STORE_DIR`。队列写入用于避免阻塞主响应；队列已满或持久失败时，
存储事件可能被丢弃并记录日志。

是否保存请求正文和供应商响应由环境变量控制。敏感字段在持久化前会经过清理。

## 原生 Responses 归属

原生 Responses 对象属于创建它的上游账号。`response_routes` 以认证范围和 response ID
为范围，保存其渠道、状态和用量记账状态。API Key 范围只记录 `token_id`；账户范围
只记录 `owner_id`，网页对话与同账户 OAuth 授权使用后者。两种范围不能互相访问。
资源后续操作依赖这条记录返回正确上游。

具体数据位置和查询方式见[日志、用量与数据存储](../operations/observability.md)。
