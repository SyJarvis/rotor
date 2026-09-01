# Session-aware Routing 设计

## 状态

- 日期：2026-08-19
- 目标客户端：Codex、Claude Code、OpenCode，以及直接调用 Rotor API 的客户端
- 结论：Rotor 的调度单位应从单次 request 提升为活跃 session；KV Cache 是路由收益信号，不是 Rotor 保存的上下文
- 进度：阶段 1、阶段 2、阶段 3 和阶段 4A 只读评估层已完成；下一步是积累真实流量并决定是否值得做 shadow 实验

## 产品边界

Rotor 定位为面向 Coding Agent 的自托管 LLM Gateway：负责协议保真、渠道选择、会话亲和、故障迁移、容量与成本观测，并解释每一次路由决策。

Rotor 保持以下职责：

- 原生兼容 OpenAI Responses、Chat Completions 和 Anthropic Messages；
- 聚合多个 provider、账号、API Key 和部署；
- 在语义兼容的渠道之间做 session 级负载均衡和 fallback；
- 记录 request、attempt、usage、cache、capacity 和 routing 事实；
- 将 provider 原生有状态对象固定到创建它的渠道和账号。

Rotor 不承担以下职责：

- 不编排主 Agent、子 Agent 或多模型协作；
- 不合并、改写或自动迁移不同模型的对话历史；
- 不把 KV Cache 当成可恢复的会话记忆；
- 不默认插入 `cache_control`、缓存断点或修改客户端 prompt；
- 不在事实数据可靠前启用不可解释的综合评分或 ML 路由。

ConversationStore 是可选的审计与调试旁路，不是路由所依赖的上下文来源。客户端仍负责构造每次请求所需的完整历史；provider 原生 `previous_response_id` / conversation state 是另一类必须固定上游归属的状态。

## 已验证的客户端信号

2026-08-18 使用本机客户端和无外网回环服务捕获了脱敏请求结构：

| 客户端 | 版本 | Session 信号 | Thread 信号 | Cache 信号 |
| --- | --- | --- | --- | --- |
| Codex | 0.147.0 | `session-id` | `thread-id` | `prompt_cache_key` |
| Claude Code | 2.1.98 | `x-claude-code-session-id` | 暂无独立字段 | `cache_control`；metadata 也含 session |
| OpenCode | 1.17.18 | `x-session-affinity` / `x-session-id` | 暂无独立字段 | 部分请求带 `prompt_cache_key` |

Codex 的主线程和子线程可以共享 session，但使用不同 thread；因此 Rotor 必须区分 session affinity 和 thread observation。上游缓存仍以 provider 的精确前缀规则为准，cache key 不能替代消息历史。

## 标识模型

内部语义分为五个互不替代的标识：

| 标识 | 用途 |
| --- | --- |
| `tenant_id` | Rotor Token / 用户隔离；防止不同租户的同名 session 相撞 |
| `session_id` | 一次 Coding Agent 会话；默认路由与租约粒度 |
| `thread_id` | 主 Agent、子 Agent 或并行任务；用于 trace，不默认拆散缓存亲和 |
| `prompt_cache_key` | provider 缓存路由提示；透传和观测，不视为会话记忆 |
| `request_id` | 一次请求及其所有 fallback attempt 的关联键 |

有效 affinity key 为：

```text
tenant_id × session_id
```

模型已作为路由入口参数参与候选选择；未来 Session Lease 的持久化键应为：

```text
tenant_id × session_id × logical_model
```

Session 解析优先级：

1. `X-Conversation-Id` / `X-Rotor-Session-Id`；
2. 已知 Coding Agent session header；
3. Responses conversation 或 metadata 中的显式 session；
4. `prompt_cache_key`；
5. 兼容旧行为的 request `user` / Anthropic metadata `user_id`；
6. 都不存在时不启用 affinity。为 ConversationStore 生成的归档 ID 不得伪装成有效 session。

## Channel 之外的资源边界

一个 Channel 当前同时承载 credential、endpoint、protocol、model mapping 和权重，但它不一定是 provider 的真实资源边界。后续配置需要逐步表达：

- `cache_scope`：切换后是否仍可能复用同一 provider cache；
- `capacity_scope`：哪些 Channel 共享 RPM、TPM、并发或账号额度；
- `billing_scope`：哪些 Channel 共享余额、套餐或账单；
- `tariff`：provider model 在某个生效时间段的 read/write/miss/output 单价。

第一版资源 scope 和 Tariff 均已作为可选 Channel 配置实现。相同类型、相同显式 scope ID 的 Channel 被视为共享该资源；没有明确信息时使用 `channel:<id>` 分别隔离，不猜测不同 API Key 是否属于同一账号。scope 是管理员声明的边界，不代表 Rotor 能保证 provider cache 必然命中。

## 路由状态机

### 新 Session

先应用硬约束，再选择渠道：

1. 模型、协议和能力兼容；
2. 渠道启用且未处于 circuit / cooldown；
3. operator priority 等管理策略；
4. 当前容量和生效价格；
5. 在等价候选中用稳定加权散列分配。

选中后创建 Session Lease。

### 活跃 Session

租约渠道健康且仍兼容时继续使用，不因短期分数或峰谷价边界逐请求搬迁。这样把负载均衡发生点放在 session 开始处，而不是每个 turn。

### 失败迁移

- 请求尚未开始输出且错误允许 fallback：尝试下一渠道；成功后把租约迁到成功渠道。
- 流已经开始：不能安全重放本次请求；记录失败并影响下一 turn 的租约。
- 临时 rate limit：遵守 `Retry-After` / reset，按 scope 冷却。
- quota / billing exhausted：打开长 circuit，等待人工处理或可信的 provider reset 信号。
- native Responses state chain：继续固定原渠道；上游不可用时返回明确的 state binding 错误，不跨账号伪迁移。

### 恢复与峰谷价

原渠道恢复后不立即抢回活跃 Session，因为 fallback 渠道此时可能已有热缓存。租约在空闲 TTL 到期后重新选择。价格进入新时段时只影响新 Session 和已过期租约。

缓存 TTL 不能全局硬编码：Anthropic 默认 5 分钟并可选 1 小时；OpenAI 和其他 provider 有不同规则；DeepSeek 为 best-effort 长时缓存。Lease idle TTL 应允许按 provider/model 配置，并与 cache TTL 解耦。

## 四阶段路线

### 1. Session 身份归一化

- 识别 Codex、Claude Code、OpenCode 的稳定 session/thread 信号；
- 保留 `X-Conversation-Id` 作为显式最高优先级入口；
- affinity 按 Rotor Token 命名空间隔离；
- 没有稳定 session 时不启用 affinity；
- routing trace 记录 session 来源、thread 和 cache key 是否存在。

验证：相同客户端 session 的连续请求得到相同 affinity key；不同 Rotor Token 不相撞；超长不可信标识被稳定压缩到数据库边界内。

### 2. Cache、Cost 与 Capacity 事实正确

统一 UsageLedger 至少记录：

- uncached input tokens；
- cache read tokens；
- cache write tokens，并保留 TTL 分类；
- authoritative total input/output；
- provider/model/protocol/channel；
- rate-limit remaining/reset 和 `Retry-After`；
- 请求发生时实际采用的 tariff version 与价格时段。

Anthropic 总输入为 `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`；DeepSeek 使用 hit/miss 字段；OpenAI 保留 `cached_tokens` 和支持模型返回的 `cache_write_tokens`。不能用一个 provider 的字段语义套用全部渠道。

阶段 2 分为三个可独立验证的增量：

- [x] **2A Cache Usage 事实归一（2026-08-19）**：统一保存 authoritative total input、uncached input、cache read、cache write、5m write 和 1h write；覆盖流式、非流式、协议转换、RequestLog、UsageLedger、聚合 API 和 request trace。旧数据标记为 usage schema v1，新记录为 v2，不对旧 Anthropic 数据做不可靠回填。
- [x] **2A2 Cache scope（2026-08-19）**：用显式 `cache_scope` 描述可能共享 provider cache 的 Channel；未配置或配置非法时按 Channel 隔离。它只表达路由成本边界，不把 cache 当作可迁移状态。
- [x] **2B1 Capacity 响应事实（2026-08-19）**：从成功与失败响应中提取所有名称包含 `ratelimit` 的 Header 和 `Retry-After`，写入 RequestLog、UsageLedger 与 request trace；只观测，不参与路由，不保留认证等无关 Header。
- [x] **2B2 Capacity scope（2026-08-19）**：用显式 `capacity_scope` 描述共享 RPM、TPM、并发和账号额度的 Channel；响应 Header 不用于自动推断。生效 scope 写入 routing decision、RequestLog、UsageLedger 和 request trace，但尚不驱动共享 cooldown。
- [x] **2C1 Tariff 与 Cost 事实（2026-08-19）**：实现显式、可版本化、带 IANA 时区和每日生效时段的 Channel tariff；按 provider model、logical model、`*` 的优先级匹配，区分 uncached input、cache read、cache write/5m/1h 和 output。实际成功上游 attempt 开始时锁定生效时段，保存 tariff snapshot 与计算结果；未配置、配置无效、缺少已使用 Token 桶的费率均保留明确状态，不内置供应商价格。
- [x] **2C2 Billing scope（2026-08-19）**：用显式 `billing_scope` 描述共享余额、套餐和账单的 Channel；不同币种分别聚合，不在没有汇率事实时相加或换算。

验证：三种 provider usage fixture 的归一结果符合各自官方字段语义；缓存命中率不超过 100%；峰谷费率按成功 attempt 开始时间、配置时区和跨午夜边界选择；旧数据库迁移幂等；混合币种不产生伪总价；scope 默认隔离、相同显式 ID 归组，非法旧配置保守回退隔离。

### 3. Session Lease

- [x] **持久化租约（2026-08-19）**：用数据库唯一键保存
  `(Rotor Token, session_id, logical_model) -> channel`，路由前只读有效租约，成功后才提交状态；
- [x] **稳定与迁移（2026-08-19）**：租约渠道仍健康且兼容时排在首位；fallback
  成功后迁移到实际成功渠道，cooldown 结束不会自动跳回旧渠道；
- [x] **事件事实（2026-08-19）**：追加记录 `assigned`、`renewed`、`migrated`、
  `expired` 及原因，并通过 request trace 返回；
- [x] **空闲过期（2026-08-19）**：默认 idle TTL 为 900 秒，可全局配置，也可在
  Channel `extra` 中按渠道或 logical model 覆盖；Lease TTL 与 provider cache TTL 解耦；
- [x] **多 worker 一致性（2026-08-19）**：数据库唯一约束、行锁与保存点处理并发首次
  绑定，不依赖单进程内存；
- [x] **协议入口（2026-08-19）**：Chat、Anthropic、Images 和 Responses 均接入；
  queued/background Responses 在上游接受并建立对象归属时绑定，轮询只补 usage；原生
  `previous_response_id` state chain 继续固定原渠道，不用归档 ID 制造虚假租约。

新 Session 当前仍使用健康/兼容性、管理优先级、现有策略和权重分配。Capacity 与
Tariff 已记录但不直接进入首选评分；是否让它们影响新 Session 属于阶段 4 的数据门槛，
避免在没有收益证据时引入逐请求价格路由。

验证：同 Session 连续请求保持渠道；fallback 后保持新渠道；租约空闲过期后才重新选路；
native state chain 永不跨渠道；两个数据库会话并发首次绑定最终只有一条权威租约。

### 4. 数据驱动的 Cache-aware Cost Routing

只有前三阶段产生的线上事实证明有收益时才启用。迁移决策比较：

```text
继续当前渠道的预期成本
vs.
新渠道预期成本 + cache 重建成本 + 迁移延迟/失败风险
```

必须加入最短驻留时间、收益阈值和滞回，避免渠道抖动。默认只在 hard failure、quota exhausted 或 lease expiry 时迁移；价格优化首先作用于新 Session。

- [x] **4A 只读评估层（2026-08-19）**：Control API 与 MCP 按时间窗口和 logical
  model 汇总 Session 覆盖、租约采用/迁移、cache read/write/uncached、费用覆盖、
  fallback、429/5xx，并按 `channel × tariff version × tariff period × currency` 输出
  可审计 cohort；不返回 Session ID，不生成综合分数，不修改路由；
- [ ] **4B Shadow 对照**：在不改变实际选路的前提下记录候选策略本会选择的 Channel，
  比较现行 Session Lease 与候选策略的成本、cache 重建、延迟、失败和迁移；
- [ ] **4C 显式启用**：只有受控对照持续证明净收益，才增加最短驻留、收益阈值、滞回
  和 operator 开关；否则不实现或永久保持关闭。

4A 的 `facts_complete_for_evaluation` 只是硬事实门槛。没有 routing decision、稳定
Session、Lease 事件、成功 provider usage，usage v2 / cost 覆盖不完整，或混合币种，
都会阻止比较。门槛通过也不等于收益成立：样本必须按 logical model 与同币种 cohort
分析，并跨多个窗口观察，不能用全局单一 cache 命中率替代对照。

验证指标：有效 session 覆盖率、session migration 次数、cache read/write/uncached 比例、实际输入成本、首事件延迟、429/5xx、fallback 成功率。若数据不能证明优于简单 Session Lease，则永久保留简单策略。

## 暂缓事项

- 暂缓 Session/Cache 大屏和复杂时间线，先保证底层事实正确；
- 暂缓把 cache utility 或 cost 加入 adaptive score；评估接口已落地，但尚未经过真实流量和 shadow 对照验证；
- 暂缓自动缓存断点和 prompt 重写；
- 暂缓跨实际模型的自动 fallback；需要先定义语义兼容的 model pool；
- 暂缓把 MindAgent 或多 Agent 编排并入路由内核。

## 资料

- [OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [OpenAI Rate Limits](https://developers.openai.com/api/docs/guides/rate-limits)
- [Anthropic Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Anthropic Rate Limits](https://platform.claude.com/docs/en/api/rate-limits)
- [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)
- [DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek Rate Limit & Isolation](https://api-docs.deepseek.com/quick_start/rate_limit/)
