# Rotor 问题清单与待办（2026-08-18）

本文档整理当前代码排查发现的问题、历史遗留待办和新方向。
与 `docs/roadmap.md`（长期路线图）互补：这里聚焦**具体问题和工作项**，
做完即勾掉。行号以 2026-08-18 的 main 分支为准，动手前需重新核对。

## 一、安全（建议最优先）

### A1. ~~Admin API 无鉴权~~（2026-08-25 已修）
- **位置**：`src/rotor/api/admin/` 全部端点（channels、tokens、mcp_control_keys、logs、settings、mindagent）
- **问题**：所有 `/api/admin/*` 端点没有任何鉴权依赖。RateLimitMiddleware 还主动跳过 admin 前缀。任何能访问端口的人都可以：增删渠道、**直接看到上游 API Key**、创建 token、改路由设置。
- **影响**：严重。rotor 一旦暴露到非本机网络（Docker 部署 `-p 8000:8000` 就是），等于上游 key 全部泄漏。
- **修复**：首次启动创建 `admin` 用户，密码仅保存 scrypt 哈希；首次登录强制改密；
  浏览器使用服务端 HttpOnly Session 和 CSRF；全部现有 admin router 统一挂鉴权依赖；
  登录失败窗口和锁定状态持久化，并追加记录登录、限流、改密和退出认证审计事件。

### A2. ~~channel test 端点回传上游原始报文~~（2026-08-18 已修）
- ~~**位置**：`src/rotor/api/admin/channels.py` 的 test 端点，`error=str(exc)` / `exc.response.text[:500]`~~
- ~~**问题**：上游错误响应原文可能包含敏感信息（部分代理会把鉴权头回显在错误里）。~~
- **修复**：新增 `_safe_upstream_error_text()`（复用 `format_error_message`/`upstream_error_payload` 清洗 + channel.key 兜底替换），probe-models 两处 detail 和 test 端点两处 `error=` 全部走它。

## 二、稳定性 / 正确性

### B1. ~~流式请求 http_client 泄漏~~（2026-08-18 已修，含 D3）
- ~~**位置**：`src/rotor/api/v1/chat.py` `chat_completions` 的 `finally`：`if not request.stream: await http_client.aclose()`~~
- ~~**问题**：流式请求依赖下游 handler 关闭 client；但当**所有候选渠道在 `adapter.make_request()` 阶段就抛错**时（走 except 分支 continue/raise），client 永不关闭。responses.py 同病（7 月排查 /v1/responses 时已记录，至今未修）。~~
- **修复**：chat.py / responses.py / anthropic.py / images.py（D3）统一改为 `streaming_response_returned` handoff 标志 —— 流式成功返回时所有权移交生成器，其余一切路径（含流式 make_request 全失败、`_record_failure` 自身抛错）finally 统一 `aclose()`。

### B2. ~~流式 overloaded 不触发路由自愈~~（2026-08-18 已修）
- ~~**位置**：适配器（`adapters/protocol/anthropic.py`、`responses.py`）把上游 SSE error 事件包成 `RuntimeError` 抛出 → `normalize_upstream_error`（`core/exceptions.py`）落 else 分支 → `fallback_allowed=False` → `mark_unavailable` 不触发~~
- ~~**问题**：上游 529/overloaded 发生在 SSE 流中途时，本次无法换渠道（已下发 200），且路由层完全看不到，**下一个请求还会打到同一过载渠道**。~~
- **修复**（见记忆文档 `project_stream_overloaded_routing_gap.md`）：
  1. ✅ `core/exceptions.py` 新增 `UpstreamOverloaded` + `is_overload_error_signal()`（显式 type/`rate_limit` 前缀/`overloaded` 文案），normalize 判 `UPSTREAM_AVAILABILITY` + `fallback_allowed=True` + `retry_same_channel=False`
  2. ✅ anthropic/responses 适配器按判定改抛新异常；openai 兼容流中途 error chunk 在 `chat.py` handler 层检测（一处覆盖 kimi/zhipu/minimax 覆写副本），只映射过载/限流
  3. ✅ 流中途失败（已发 200）：本次不重发但 `mark_unavailable`（默认 30s cooldown 回切）；流开始前失败（非 200）本来就走 fallback 重试
  4. 附带修复：openai_responses 转换路径此前会静默吞掉上游 error chunk 并以 `response.completed` 收尾，现在正确走 `response.failed`
  5. attempt trace 记录：chat/anthropic 流式 except 的 isinstance 元组补 `UpstreamOverloaded`
- **遗留**：线上频率观察（请求日志 `error_code=UpstreamOverloaded`）与 cooldown 30s 是否合适的调参。

### B3. ~~流式失败路径不喂 adaptive 路由信号~~（2026-08-18 核实：不存在）
- ~~**位置**：`gateway/accounting.py` 的 `observe_result` 只挂在 `record_success` / `record_failure` 部分调用路径；流式中途失败的某些分支没有走到~~
- **核实结论**：`observe_result` 内嵌在 `accounting.record_success` / `record_failure` 里（`gateway/accounting.py:116/176`），不是散落调用。逐路径核对 chat/anthropic/responses/images 的流式失败分支，全部走 `record_failure` → adaptive 信号全覆盖。描述已过时，关闭。

### B4. /v1/responses 协议遗留问题（2026-08-18 复核：仅剩 1 条）
- ~~流式错误事件用 Chat Completions 格式~~（已修：`chat.py` except 块按 `request_protocol == "openai_responses"` 发 `response.failed`，转换路径走 `stream_transform.fail()`）
- ~~非流式缺 `status: "completed"`~~（已修：`chat_response_to_responses` 顶层有该字段）
- ~~http_client 泄漏~~（2026-08-18 B1 已修）
- ~~`tools` / `tool_choice` 未接~~（已实现：`responses_request_to_chat` 已转换两者；`previous_response_id` 走 native 路由 + `ResponseRoute` 状态链，转换路径不支持是有意设计）
- ~~`ResponsesStreamTransform.feed` 忽略 tool_calls~~（已实现：feed 处理 `delta.tool_calls` 并发 `function_call` 事件序列）
- **唯一遗留**：`ResponsesStreamTransform.feed` 不处理 `reasoning_content`（经转换渠道调思考模型时 Codex 端看不到 reasoning）。做法：加 `response.reasoning_text.delta` / reasoning item 映射。
- **状态**：等 Codex 实测反馈——若思考模型经转换渠道的 reasoning 丢失影响体验再做，否则永久关闭。

### B5. conversation_store 关闭丢事件
- **位置**：`conversations/store.py`
- **问题**：两个丢点——`_enqueue` 队列满直接丢（QueueFull 只打日志）；重试 3 次耗尽后直接丢，无补偿。正常 shutdown 不丢（sentinel + drain 排空）。
- **影响**：低（会话存档是旁路数据，不影响计费/主流程）。
- **方案**（2026-08-18 定）：只做可观测（丢弃计数 + 日志带累计值），不做 spool 回放——旁路数据不值得引入回放复杂度。
- **状态**：丢弃计数已加（2026-08-18）。回放机制永久不做，除非计数显示线上实际在丢。

## 三、性能 / 可扩展性

### C1. RateLimitMiddleware 三重问题
- **位置**：`core/middleware.py:93`
- 内存 dict 按 IP 累积时间戳，无全局清理 → 缓慢内存增长
- 多 worker 部署时各进程独立计数，限流形同虚设
- admin 前缀直接跳过（在 A1 修好前这反而是唯一"防线"，修 A1 时一并处理）
- **方案**：短期加 LRU/TTL 清理即可；多 worker 场景再考虑共享存储。

### C2. RequestLog 缺复合索引
- 高频查询按 `(model, created_at)` / `(channel_id, created_at)` 组合过滤（stats、timeseries），目前只有单列索引。
- 数据量上万后 stats 变慢可再加：`(model, created_at)`、`(channel_id, created_at)`。
- 注意：需要走 SQLite `CREATE INDEX IF NOT EXISTS` 启动迁移（本次测速功能已验证过该模式可行）。

### C3. SQLite 并发写锁
- WAL + busy_timeout 已大幅缓解（2026-07-15 修），高并发下仍可能偶发。
- 方向：写入聚合（accounting 批量落库）/ 迁移 PostgreSQL（roadmap P0 已列双库兼容）。

## 四、低优先级杂项

- D1. token 过期并发更新竞态（`core/deps.py` 的 `get_current_token`）——影响小
- D2. mindagent `_run_mindagent` 资源清理路径多重 try-finally 加固——影响小
- ~~D3. images 端点异常路径 http_client 关闭不完整~~（2026-08-18 已随 B1 修复）

## 五、新方向：面向 Coding Agent 的 Session-aware Routing

详细边界、状态机和验证标准见 [`docs/session-aware-routing-design.md`](session-aware-routing-design.md)。

**定位**：Rotor 是 Coding Agent 与多家模型渠道之间的 session-aware gateway，不是 Agent 编排器。它负责协议保真、渠道选择、会话亲和、故障迁移、容量与成本观测；不保存可恢复的 KV Cache，不合并不同模型的上下文，也不编排多个 Agent 同时工作。

**已确认的事实**：

- Codex 0.147.0 会发送 `session-id`、`thread-id` 和 `prompt_cache_key`；
- Claude Code 2.1.98 会发送 `x-claude-code-session-id`，metadata 也包含 session，并使用 `cache_control`；
- OpenCode 1.17.18 会发送 `x-session-affinity` / `x-session-id`，部分请求带 `prompt_cache_key`；
- Rotor 原先把随机生成的 `conversation_id` 当 affinity key，Codex/OpenCode 等未显式传 `X-Conversation-Id` 时实际上无法稳定粘性；
- cache、容量和账单范围不一定等于 Channel：同账号的多个 key 可能共享容量或账单，不同 endpoint/API key 是否共享 provider cache 也不能靠 Rotor 猜测。

**四阶段实施顺序**：

- [x] **阶段 1：Session 身份归一化**（2026-08-18）
  - 识别 Codex、Claude Code、OpenCode 和 Rotor 显式 session header；
  - affinity key 按 Rotor Token 命名空间隔离；
  - 没有稳定 session 时不再用随机归档 ID 制造虚假 affinity；
  - trace 记录 session 来源、thread 和 cache key 是否存在；
  - 8 个新增单元测试覆盖优先级、fallback、隔离和超长 ID。
- [x] **阶段 2：先把 Cache、Cost、Capacity 事实记对**（2026-08-19）
  - [x] **2A Cache Usage 归一（2026-08-19）**：分开记录 uncached input、cache read、cache write/5m/1h 和 authoritative total；修正 Anthropic total input，适配 DeepSeek hit/miss 与 OpenAI cached/write；贯通流式、非流式、协议转换、两张用量表、聚合 API 和 trace；
  - [x] 旧数据库启动迁移幂等补列；历史记录标记 usage schema v1，新记录为 v2，不猜测回填旧 Anthropic 数据；
  - [x] **2B1 Capacity 响应事实（2026-08-19）**：成功与失败响应均保存 rate-limit remaining/reset 和 `Retry-After` 等上游响应头；只保留容量相关 Header，不记录认证信息，暂不参与路由；
  - [x] **2A2 Cache scope（2026-08-19）**：显式配置可能共享 provider cache 的 Channel；未配置或非法时按 Channel 隔离；
  - [x] **2B2 Capacity scope（2026-08-19）**：显式配置共享 RPM、TPM、并发或账号额度的 Channel 范围；不从响应头自动推断，当前只记录、不驱动共享 cooldown；
  - [x] **2C1 Tariff/Cost 事实（2026-08-19）**：Channel `extra.tariffs` 支持 provider model、logical model 和通配符费率，区分 input/cache read/cache write TTL/output，支持时区与跨午夜峰谷时段；按实际成功上游 attempt 开始时间锁定版本、时段和费率快照；未配置、无效或缺项价格分别保留状态，不内置猜测价格；
  - [x] **2C2 Billing scope（2026-08-19）**：显式配置共享余额、套餐或账单的 Channel 范围；不同币种只分别聚合，不做隐式换算；
  - 先做聚合 API 和可验证指标，暂不投入复杂前端大屏。
- [x] **阶段 3：Session Lease（2026-08-19）**
  - 数据库持久化 `(Rotor Token, session, logical_model) -> channel`，唯一约束和并发首次绑定测试保证多 worker 只有一条权威租约；
  - 路由前只读有效租约，成功后才 assigned/renewed/migrated；失败尝试不污染绑定；
  - fallback 成功后迁移租约，渠道恢复时不立即抢回；默认空闲 900 秒过期后再重新选择，支持全局、Channel 和 model 覆盖；
  - 事件及原因进入 request trace；Chat、Anthropic、Images、Responses 已接入；
  - provider 原生 `previous_response_id` / conversation state 永不跨渠道伪迁移，归档 ID 不作为租约 ID；
  - Capacity 与 Tariff 暂不参与新 Session 评分，保留到阶段 4 用真实数据决定。
- [ ] **阶段 4：有数据门槛的 Cache-aware Cost Routing**
  - [x] **4A 只读评估层（2026-08-19）**：Control API 与 MCP 按窗口/model 汇总
    Session 覆盖、Lease 事件、迁移、cache、费用、fallback、429/5xx，并按
    Channel/费率版本/峰谷时段/币种输出 cohort；硬事实不完整时给出 blocking reason，
    不返回 Session ID、不做综合评分、不修改路由；
  - [ ] **4B Shadow 对照**：先记录候选策略而不改变真实选路，验证净成本、cache 重建、
    延迟、失败和迁移是否持续优于简单 Session Lease；
  - [ ] **4C 显式启用**：只有对照证据成立才加入驻留时间、收益阈值、滞回和人工开关；
    否则永久保持简单策略；
  - 比较“留在当前渠道”和“新渠道价格 + cache 重建 + 延迟/失败风险”；
  - 峰谷价优先影响新 Session，不在活跃 Session 中逐请求抖动；
  - 只有真实指标证明优于简单 Session Lease 时才开启，否则永久保持简单策略。

**暂不做**：自动插入缓存断点、修改客户端 prompt、跨实际模型自动 fallback、多模型/多 Agent 上下文编排、基于不完整 cache 数据的综合评分。

## 六、已归档

- ~~TTFT/TPS 测速功能~~（2026-08-18 实现后当天移除：思考模型的 reasoning 阶段使指标语义混乱，各 provider 关思考参数不统一，暂无好设计。DB 残留列无害保留。若重做：探测关思考测纯管道速度 + 被动数据 reasoning 算首 token）
- ~~UA 客户端识别（日志层）~~（已上线：LoggingMiddleware 记 UA，等真实流量样本积累后再做 client_name 解析与持久化）
- ~~request_origin 死代码修复~~（已完成：MindAgent 进程内调用现在正确标记 `rotor_agent`）

## 建议动手顺序

1. **A1 admin 鉴权**（安全问题，越早越好）
2. ~~**B1 + D3 http_client 泄漏**~~（2026-08-18 完成）
3. ~~**A2 test 端点脱敏**~~（2026-08-18 完成）
4. ~~**B2 overloaded 路由自愈**~~（2026-08-18 完成）
5. ~~**Session-aware Routing 阶段 1：身份归一化**~~（2026-08-18 完成）
6. ~~**Session-aware Routing 阶段 2A、2B1、2C1：Cache、Capacity 响应与 Tariff/Cost 事实**~~（2026-08-19 完成）
7. ~~**Session-aware Routing 阶段 2A2/2B2/2C2：资源 scope**~~（2026-08-19 完成）
8. ~~**Session-aware Routing 阶段 3：Session Lease**~~（2026-08-19 完成：持久化租约、成功后迁移、空闲过期、事件与四协议入口）
9. **Session-aware Routing 阶段 4B：Shadow 对照**（4A 只读评估层已完成；先积累真实 Session Lease 指标，再决定是否实现）
