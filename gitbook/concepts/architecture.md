# 系统架构

Rotor 的核心是一条协议无关的网关请求链路，外加记录用量、会话和路由状态的
持久化旁路。

```text
OpenAI / Anthropic 客户端
          │
          ▼
FastAPI 协议入口
          │
          ├── Rotor Token 鉴权与渠道访问限制
          ├── 模型、协议和能力筛选
          ▼
RoutingEngine
          │
          ├── priority / weight / affinity / adaptive
          └── retryable failure cooldown
          ▼
Provider Adapter / Protocol Converter
          │
          ▼
上游模型 API

请求结果 ──► RequestLog / UsageLedger / RoutingDecision
会话事件 ──► ConversationStore
原生 Responses ID ──► ResponseRoute
```

## API 层

FastAPI 应用提供 Chat Completions、Responses、Images、Anthropic Messages 和
管理接口。客户端接口通过 Rotor Token 鉴权；管理接口面向受信任的部署边界。

## 路由层

路由器先排除禁用、能力不匹配或正在冷却的渠道，再按当前策略生成有序候选集。
请求处理器依次尝试候选渠道，只有符合故障转移条件的异常才继续下一个渠道。

## Adapter 层

Adapter 封装上游 URL、鉴权头、模型映射、请求格式和流式响应。Channel 的
`protocol` 决定是否使用原生 Responses adapter；其他 provider 由 adapter factory
按 `type` 选择。

## 状态层

当前版本使用文件型 SQLite；关系数据库保存渠道、Token、日志、usage ledger、路由决策
和 Responses 归属，会话正文由异步 ConversationStore 写入文件系统。启动迁移路径只
接受 SQLite，其他 `DATABASE_URL` 会在启动阶段明确报错。

下一步可以分别阅读[协议兼容与转换](protocols.md)和[记账、会话与状态](accounting-and-state.md)。
