# 记账、会话与状态

Rotor 将请求审计、Token 配额、会话正文和原生 Responses 归属分成不同的数据
结构，避免把它们混成单一日志。

## 请求日志和用量账本

`request_logs` 面向管理页查询，记录模型、渠道、成功状态、延迟、错误和基本用量。
`usage_ledger` 是按请求追加的用量记录，包含客户端/上游协议、映射后的模型、
细分 Token、用量来源和状态。

成功请求会增加 Rotor Token 的请求数、Token 数和已用配额。上游未提供 usage 时，
`usage_source` 为 `missing`，不会用估算值冒充供应商数据。

## 路由决策

`routing_decisions` 保存每次生成的候选渠道、选中渠道、策略版本、能力要求、评分和
特征快照。它通过 `request_id` 与 usage ledger 关联。

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
