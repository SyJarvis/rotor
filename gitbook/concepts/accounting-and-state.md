# 记账、会话与状态

Rotor 将请求审计、Token 配额、会话正文和原生 Responses 归属分成不同的数据
结构，避免把它们混成单一日志。

## 请求日志和用量账本

`request_logs` 面向管理页查询，记录模型、渠道、成功状态、延迟、错误和基本用量。
`usage_ledger` 是按请求追加的用量记录，包含客户端/上游协议、映射后的模型、
细分 Token、用量来源和状态。

成功请求会增加 Rotor Token 的请求数、Token 数和已用配额。上游未提供 usage 时，
`usage_source` 为 `missing`，不会用估算值冒充供应商数据。

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

`session_leases` 是多 worker 共享的活跃会话路由状态，键为 Rotor Token、Session ID
和 logical model。`session_lease_events` 是追加式转换事实，通过 request ID 与路由、
attempt 和 usage 关联。租约不包含 prompt 或 provider KV Cache，也不替代客户端上下文。

## 会话存储

设置 `X-Conversation-Id` 后，请求生命周期事件可由 ConversationStore 异步写入
`CONVERSATION_STORE_DIR`。队列写入用于避免阻塞主响应；队列已满或持久失败时，
存储事件可能被丢弃并记录日志。

是否保存请求正文和供应商响应由环境变量控制。敏感字段在持久化前会经过清理。

## 原生 Responses 归属

原生 Responses 对象属于创建它的上游账号。`response_routes` 以 Rotor Token 和
response ID 为范围，保存其渠道、状态和用量记账状态。资源后续操作依赖这条记录
返回正确上游。

具体数据位置和查询方式见[日志、用量与数据存储](../operations/observability.md)。
