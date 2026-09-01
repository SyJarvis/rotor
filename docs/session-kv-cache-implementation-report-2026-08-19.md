# Rotor Session / KV Cache 改造实施与交接记录

## 文档信息

- 日期：2026-08-19
- 工作目录：`/Users/whoami/research/rotor`
- 目标版本：`0.3.0`
- 文档范围：本轮围绕 Coding Agent、Session、KV Cache、渠道切换、可观测性和稳定性完成的设计、代码、测试、真实数据验证及后续计划
- 当前决策：优先保证简单可靠的 Session Lease；暂不做按价格切换渠道，也不继续建设 token 花费统计或成本路由
- 工作树状态：改动尚未提交；创建本文档前有 74 个已跟踪文件被修改、18 个新增文件未跟踪

## 一、结论

Rotor 当前最合适的定位是：

> 面向 Coding Agent 的 session-aware LLM gateway。Rotor 负责协议保真、渠道选择、会话亲和、故障迁移和路由事实记录，但不保存或迁移 KV Cache，不接管 Coding Agent 的上下文，也不编排多模型 Agent。

已经实现的核心运行方式如下：

```text
Coding Agent request
  -> 解析客户端 Session / Thread / Cache 信号
  -> 按 Rotor Token 隔离 Session
  -> 查询 (token, session, logical_model) 的有效 Lease
  -> Lease 渠道健康且兼容：优先继续使用
  -> 无 Lease：按现有 priority / weight / affinity 选择
  -> 上游成功：记录 attempt、usage，并 assigned / renewed / migrated Lease
  -> 上游失败或客户端取消：记录终态，不修改 Lease
  -> fallback 成功：Lease 迁移到实际成功渠道
  -> 空闲 TTL 过期：下一次成功后重新分配
```

这条路径避免了活跃 Coding Agent 会话因逐请求负载均衡而频繁丢失 provider KV Cache，同时保留必要的失败切换能力。

## 二、产品边界与已经做出的取舍

### Rotor 负责

- 原生或兼容接入 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 和 Images；
- 聚合多个 provider、账号、Key 和 endpoint；
- 识别 Codex、Claude Code、OpenCode 等客户端的稳定 Session 信号；
- 在同一个 logical model 的兼容 Channel 之间保持 Session Lease；
- 在请求尚未开始输出且错误允许 fallback 时切换渠道；
- 记录 routing decision、upstream attempt、usage、cache、capacity、resource scope 和 Lease 事件；
- 固定 provider 原生 Responses state chain 的上游归属；
- 提供 Control API 和 MCP 只读评估能力。

### Rotor 不负责

- 不保存 provider 内部 KV Cache；
- 不把 KV Cache 当作可恢复的长期记忆；
- 不自动把一个渠道的 KV Cache 迁移到另一个渠道；
- 不修改 Coding Agent 的对话历史或自动插入缓存断点；
- 不合并不同模型各自的上下文；
- 不编排多个模型或多个 Agent 同时工作；
- 不在本阶段按峰谷价格逐请求切换渠道；
- 不在事实不完整时生成综合成本分数或自动策略建议。

多模型同时工作时，每个 Coding Agent / thread 仍由客户端维护自己的上下文。Rotor 只根据明确的 Session 和 logical model 路由，不把模型 A 的历史注入模型 B，也不跨模型共享 Lease。

## 三、已完成的实现

### 3.1 前置稳定性与安全修复

在进入 Session Lease 前，先处理了会影响长时间 Coding Agent 使用的基础问题：

1. 修复 Chat、Responses、Anthropic 和 Images 流式请求在所有候选渠道于 `make_request()` 阶段失败时的 `http_client` 泄漏。
2. 新增流式 overload 识别：Anthropic / Responses SSE error 和 OpenAI 兼容 error chunk 能映射为 `UpstreamOverloaded`。
3. 流中途 overload 不重放已经输出的请求，但会冷却该 Channel，避免下一请求继续命中过载渠道。
4. Responses 转换路径不再吞掉上游 error 并伪装为 completed。
5. Channel test / probe 返回的上游错误统一脱敏，避免 provider 回显 API Key。
6. 新增已保存 Channel 的 model probe，不需要把保存的 Key 再传回前端。
7. ConversationStore 增加队列满与重试耗尽的丢弃计数和状态接口；继续保持旁路定位，不引入 spool 回放。
8. 请求日志加入截断后的 User-Agent，作为客户端来源排查的辅助事实。

这些修改主要分布在：

- `src/rotor/api/v1/chat.py`
- `src/rotor/api/v1/responses.py`
- `src/rotor/api/v1/anthropic.py`
- `src/rotor/api/v1/images.py`
- `src/rotor/core/exceptions.py`
- `src/rotor/api/admin/channels.py`
- `src/rotor/conversations/store.py`
- `src/rotor/core/middleware.py`

### 3.2 阶段 1：客户端 Session 身份归一化

新增 `src/rotor/core/client_session.py`，统一解析客户端信号。

Session 优先级：

1. `X-Conversation-Id`
2. `X-Rotor-Session-Id`
3. Codex `session-id`
4. Claude Code `x-claude-code-session-id`
5. OpenCode `x-session-affinity` / 通用 `x-session-id`
6. Responses conversation / metadata session
7. `prompt_cache_key`
8. 兼容旧客户端的 user / metadata user ID

同时处理：

- Codex `thread-id` / `x-thread-id` 单独记录，不默认拆分 Session Lease；
- affinity key 使用 `token_id:session_id`，防止不同 Rotor Token 的同名 Session 相撞；
- 超过数据库边界的标识使用 SHA-256 稳定压缩；
- 没有稳定 Session 时不再用随机 ConversationStore ID 制造虚假 affinity；
- routing feature snapshot 记录 `session_source`、`thread_id` 和 `cache_key_present`。

当前客户端来源状态：底层已经记录 Session 来源，HTTP 日志也有 User-Agent；但 Control API / MCP 尚未按 Codex、Claude Code、OpenCode 聚合客户端来源，因此评估工具不能从缺失的聚合字段反推来源。

### 3.3 阶段 2：Cache、Capacity、Scope 与 Tariff 事实

#### Cache usage v2

`RequestLog` 与 `UsageLedger` 新增或贯通：

- authoritative prompt/input tokens；
- completion/output tokens；
- uncached input tokens；
- cache read tokens；
- cache write tokens；
- 5 分钟 cache write tokens；
- 1 小时 cache write tokens；
- reasoning / input audio / output audio tokens；
- `usage_source`；
- `usage_schema_version`。

Provider 字段按各自语义归一：

- Anthropic 总输入为 uncached input + cache creation + cache read；
- DeepSeek 使用 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`；
- OpenAI / Responses 使用 `cached_tokens` 及可用的 write details；
- 旧记录保持 usage schema v1，不对历史 Anthropic 数据做不可靠回填；新记录为 v2。

#### Capacity facts

新增 `src/rotor/gateway/provider_facts.py`：

- 从成功和失败 response 中提取名称包含 `ratelimit` 的 Header；
- 提取 `Retry-After`；
- 不保存 Authorization、Cookie 等无关敏感 Header；
- 当前只记录，不用于自动推断共享容量，也不驱动跨 Channel cooldown。

#### Resource scopes

新增 `src/rotor/core/resource_scopes.py`，Channel `extra` 可显式声明：

- `cache_scope`
- `capacity_scope`
- `billing_scope`

未配置或非法配置一律回退到 `channel:<id>`，即保守隔离。Rotor 不根据 endpoint、API Key 或响应 Header 猜测两个 Channel 是否共享 provider 资源。

#### Tariff / cost facts

新增 `src/rotor/gateway/pricing.py`，实现了显式 tariff 的底层事实计算：

- provider model、logical model、`*` 的匹配优先级；
- 版本、币种和 IANA 时区；
- 默认费率与跨午夜峰谷时段；
- uncached input、cache read、cache write、5m/1h write、output 分桶；
- 按实际成功 attempt 开始时间锁定 tariff snapshot；
- 未配置、无效或缺失费率分别记录 `unknown`、`invalid_tariff`、`incomplete_tariff`；
- 不内置任何供应商价格，不在没有汇率时合并不同币种。

当前决定是保留这部分已经实现的底层兼容能力，但不配置价格、不继续建设 token 花费统计，也不让 cost 进入路由。`AccountingService` 传给 adaptive routing 的 cost 仍为 `None`。

### 3.4 阶段 3：持久化 Session Lease

新增：

- `src/rotor/models/session_lease.py`
- `src/rotor/services/session_leases.py`
- `alembic/versions/2026_08_19_1200_f6a7b8c9d0e1_session_leases.py`

数据库权威键为：

```text
(Rotor Token, session_id, logical_model) -> channel_id
```

实现细节：

- `session_leases` 保存当前绑定、最后使用时间和过期时间；
- `session_lease_events` 追加记录 `assigned`、`renewed`、`migrated`、`expired`；
- 路由前只读有效 Lease，不在 request start 时抢先续租；
- 只有上游成功后才在 accounting transaction 中提交 Lease 变化；
- 同一渠道成功产生 `renewed`；
- fallback 后其他渠道成功产生 `migrated`；
- 空闲过期先记 `expired`，然后在成功渠道重新 `assigned`；
- 数据库唯一约束、行锁和 nested transaction 处理多 worker 首次并发绑定；
- 默认 idle TTL 为 900 秒，允许 60–86400 秒；
- 支持 Channel 级 `session_lease_idle_ttl_seconds`；
- 支持 `session_lease_idle_ttl_by_model` 及 `*` fallback；
- Lease TTL 与 provider cache TTL 解耦；
- cooldown 恢复不会自动抢回活跃 Session。

四个协议入口均已接入：

- OpenAI Chat Completions
- Anthropic Messages
- OpenAI Responses
- Images

Responses 的特殊处理：

- provider 原生 `previous_response_id` / conversation state 继续固定到创建它的 Channel；
- background / queued response 在上游接受并形成对象归属时绑定；
- 后续 polling 只补 usage，不跨渠道伪迁移；
- ConversationStore 的归档 ID 不作为 Session Lease ID。

### 3.5 阶段 4A：只读评估 API 与 MCP

新增 `src/rotor/services/session_lease_evaluation.py` 和对应 schema / route。

Control API：

```text
GET /api/control/v1/usage/session-leases
GET /api/control/v1/usage/session-leases?model=<logical-model>&start_time=<ISO8601>&end_time=<ISO8601>
```

MCP 工具：

```text
rotor_evaluate_session_leases
```

可返回：

- routing decision 数量；
- stable Session coverage；
- preferred Lease 与实际采用率；
- assigned / renewed / migrated / expired；
- continuation 与 migration rate；
- cache read / write / uncached 比例；
- fallback 请求、成功率、429 和 5xx；
- cost fact coverage 和按 Channel / tariff / period / currency 的 cohort；
- `facts_complete_for_evaluation` 及 blocking reasons。

该接口是只读事实层：不返回原始 Session ID、不生成综合分数、不推荐策略、不修改路由。

MCP 包源码、schema、client、server 和测试均有更新。若使用旧的已安装 `rotor-mcp` wheel，需要重新构建或安装并重启 MCP 客户端；如果已经安装的是包含 4A 的版本，本次最后一轮流式 bug 修复没有修改 MCP，无需再次下载。

### 3.6 前端当前能力

管理页面已经增加：

- Session affinity 开关；
- Session Lease 开关；
- Session Lease idle TTL 设置；
- 中英文说明；
- 已保存 Channel 的 model probe；
- probe loading 状态；
- Channel extra 可承载 resource scopes 和 tariffs；
- Overview 大请求数的紧凑格式化；
- ConversationStore 状态可通过管理接口读取。

当前没有专门的 Session / KV Cache 页面，也没有 Lease 时间线、客户端来源聚合或 4A 图表。现阶段通过 Control API / MCP 查看，复杂大屏继续延期。

### 3.7 最后一轮线上数据驱动 bug 修复

#### 1. 流式 cache usage 合并错误

真实 DeepSeek 流量中出现：

```text
cached_tokens + uncached_input_tokens > prompt_tokens
```

根因是流式处理对 prompt、cached、uncached 等字段分别取 `max`。Provider 先发“全部 prompt 暂视为 uncached”的快照，再发相同 prompt 总数但包含 cache hit/miss 的详细快照时，旧的 uncached 最大值不会被替换。

修复：新增 `StreamingUsageAccumulator`，输入侧的 prompt/cache breakdown 作为一个完整快照原子替换，输出 token 仍可独立递增。Chat / Responses 和 Anthropic 两条流式路径共用相同语义。

#### 2. Responses 转 Chat 的孤立 `tool_choice`

Codex Responses 请求可能只包含 hosted / namespace tools。转换为 Chat 时这些工具会被过滤，但此前仍保留 `tool_choice: auto`，导致部分 provider 返回：

```text
When using tool_choice, tools must be set
```

修复：转换后没有任何可用 function tools 时，同时移除 `tool_choice`；只要保留了 function tools，原有 tool choice 继续转换。

#### 3. `prompt_cache_retention` 模型不支持

原生 Responses 模型可能返回精确的 HTTP 400：

```text
prompt_cache_retention is not supported on this model
```

修复：只在 status=400、`param=prompt_cache_retention`、`code=invalid_parameter` 且原请求确实带该字段时，在同一 Channel 重试一次并移除该字段；保留 `prompt_cache_key`。其他 400 不重试。

#### 4. 流式异常 / 取消缺少终态 attempt

此前普通 `RuntimeError` 不会写 attempt；`asyncio.CancelledError` 又不属于 `Exception`，可能导致 routing decision 已存在但 attempt 或 conversation 仍为 started。

修复：

- 任意普通流式异常记录 `outcome=failed` 和标准化错误事实；
- 客户端取消记录 `outcome=cancelled`；
- conversation 同步进入 failed / cancelled 终态；
- failure accounting 自身出错也不能阻止 attempt 和 conversation 终态；
- Chat、Responses、Anthropic 均覆盖；
- failed / cancelled 路径不调用 `record_success`，因此不会错误 assigned、renewed 或 migrated Session Lease。

## 四、数据库与迁移

### 新表

- `session_leases`
- `session_lease_events`

### RequestLog / UsageLedger 新事实

- uncached / cached / cache write / 5m / 1h token buckets；
- usage schema version；
- capacity snapshot；
- cache / capacity / billing scopes；
- input / output / total cost；
- currency、cost status、tariff version / period / snapshot；
- provider model 和 protocol 相关 trace 字段。

### 迁移方式

- Alembic 增加 Session Lease revision；
- SQLite 启动迁移为旧 RequestLog / UsageLedger 幂等补列；
- 旧 usage 默认 schema v1，新记录为 v2；
- `alembic/env.py` 与 `src/rotor/main.py` 已导入新模型，确保 metadata 完整。

### 当前数据库风险

对 `/Users/whoami/research/rotor/rotor.db` 做只读检查时，原文件的 `PRAGMA quick_check` 报告 malformed database，主要涉及 `request_attempts` 表和索引页。当天分析使用 SQLite `.recover` 生成的 `/tmp` 恢复副本，恢复副本 `quick_check=ok`；没有修改原数据库。

这更像是复制在线 WAL 数据库时主文件、`-wal` 和 `-shm` 不一致，也可能是真实损坏。后续分析前应：

1. 停止 Rotor 后复制 DB，或使用 SQLite backup API / `.backup`；
2. 同时保留主文件、WAL 和 SHM，不能只复制主文件；
3. 每次分析副本先执行 `PRAGMA quick_check`；
4. 不在可疑原库上直接执行修复或 destructive 操作。

## 五、2026-08-19 真实流量验证

### 数据窗口与来源

- 分析窗口：Asia/Shanghai 2026-08-19 00:00 起，数据实际到约 22:26；
- 使用恢复后的只读数据库；
- 全库恢复后有约 98,343 条 routing decisions、451 条 Lease events、20 条当前 leases、102,103 条 usage ledgers、95,064 条 attempts、96,285 条 request logs；
- 当天存在大量无稳定 Session 的本地 Qwen 批量请求，因此全局 Session coverage 没有产品意义，需按 Coding Agent / logical model 分组。

### Coding Agent 来源

- Claude Code：约 354 条 routing decisions，8 个稳定 Session；
- Codex：约 105 条 routing decisions，5 个稳定 Session；
- 显式 `X-Conversation-ID`：约 5 条，2 个 Session；
- 所有观测到的交互模型 stable Session coverage 均为 100%。

### Lease 行为

- assigned：35；
- renewed：399；
- migrated：2；
- expired：15；
- preferred 425 次、实际应用 424 次，约 99.76%；
- 两次迁移分别来自 `leased_channel_unavailable` 和 `fallback_success`；
- 没有观察到因价格变化或短期评分主动搬迁。

两次迁移请求的 cache read 率只有约 0.57% 和 0.58%，直接说明跨渠道迁移会重建 KV Cache。该数据支持“活跃 Session 默认保持、只有失败或过期才迁移”的当前策略。

### Cache 与可靠性

- 稳定 Session 成功 usage 共 436 条；
- input tokens 约 21,101,700；
- cached tokens 约 12,139,284；
- 表面 cache read rate 约 57.53%；
- cache write 均为 0，说明这些 provider / 转换路径没有返回可记录的 write facts；
- DeepSeek 40 条 cached streaming rows 存在 uncached 重复累计问题，已在最后一轮修复；旧数据不会自动修正，需要新流量重新观察。

渠道和模型异常：

- Claude Code `glm-5.2`：Channel 1 成功率和延迟明显优于 Channel 2；现有 priority 已把 Channel 2 作为 fallback，配置方向正确；
- Claude Code `glm-5.3`：约 98.77% 成功，出现 1 次 upstream 500；
- Codex `glm-5.2`：约 98.21% 成功；
- Codex DeepSeek：原成功率约 81.63%，9 次 HTTP 400 均为孤立 `tool_choice`，已修复；
- 当天未观察到 429；
- `glm-5.2` 有两次 fallback request，其中一次成功迁移，另一次两个渠道都失败。

### 成本事实

- 当天成功 usage 的 `cost_status` 均为 unknown；
- 没有配置 Channel tariffs；
- 没有可比较的 costed cohorts；
- 按当前决策不继续补价格或 token 花费统计，不用这批数据做成本路由结论。

## 六、当前配置、API 和使用方式

### 默认设置

```json
{
  "routing": {
    "affinity_enabled": true,
    "session_lease_enabled": true,
    "session_lease_idle_ttl_seconds": 900
  }
}
```

关闭 affinity 时 Session Lease 同时失效。Lease 仍要求客户端提供可解析的稳定 Session；没有 Session 的普通请求继续按原策略选路。

### Channel 覆盖示例

```json
{
  "session_lease_idle_ttl_seconds": 600,
  "session_lease_idle_ttl_by_model": {
    "glm-5.2": 900,
    "*": 600
  },
  "cache_scope": "provider-account-a/cache",
  "capacity_scope": "provider-account-a/capacity",
  "billing_scope": "provider-account-a/billing"
}
```

当前主目标只需要 Lease TTL；scope 保留为事实字段，不需要为了启用 Session Lease 而配置。

### 推荐 MCP 询问方式

```text
请调用 rotor_evaluate_session_leases，评估从 <start_time> 到 <end_time>
的 logical model <model>。严格基于工具返回字段说明：
1. facts 是否完整及 blocking reasons；
2. stable session coverage 和 lease application rate；
3. assigned / renewed / migrated / expired；
4. cache read / write / uncached；
5. fallback、429、5xx。
不要根据缺失字段推断 Coding Agent 来源，也不要给出价格路由建议。
```

## 七、验证结果

### 本轮 Session / bug 定向测试

```text
78 passed
```

覆盖：

- usage 字段归一；
- 流式输入 cache snapshot 原子替换；
- Anthropic output-only usage 不覆盖 input snapshot；
- Responses hosted-only tools 清理 `tool_choice`；
- `prompt_cache_retention` 精确重试；
- Chat / Responses / Anthropic 流式 failed 和 cancelled 终态；
- failed / cancelled 携带 `lease_session_id` 时仍不进入 success / Lease 写入；
- fallback、overload、自愈和 client close。

### MCP 测试

```text
20 passed
```

### 根项目全量测试

```text
244 passed, 1 failed
```

唯一失败：

```text
tests/test_mindagent_chat.py::test_context_manager_keeps_default_system_before_history
```

实际结果把默认 system prompt 排在历史 user / assistant 消息之后，测试要求 system prompt 位于最前。该失败可以独立复现，相关 MindAgent 源码和测试均未被本轮 Session Lease 修复修改。

### 静态检查

- 本轮修改相关文件的 Ruff 检查通过；
- `git diff --check` 通过；
- `src/rotor/api/v1/anthropic.py` 在完整 F401 检查下仍有 7 个本轮之前就存在的 unused import 警告，本轮没有顺手清理无关代码。

## 八、当前完成度

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| Coding Agent Session 识别 | 已完成 | Codex、Claude Code、OpenCode、显式 Header 和 body fallback |
| Token 隔离 affinity | 已完成 | 不同 Rotor Token 不碰撞 |
| 持久化 Session Lease | 已完成 | 成功后 assigned / renewed / migrated |
| Lease 空闲过期 | 已完成 | 默认 900 秒，可按 Channel / model 覆盖 |
| 多 worker 首次绑定 | 已完成 | 唯一约束、行锁、nested transaction |
| Chat / Anthropic / Responses / Images | 已接入 | 包含 native Responses state binding |
| 失败 / 取消终态 | 已完成 | 不错误续租 |
| DeepSeek cache usage 修复 | 已完成 | 只影响修复后的新记录 |
| Codex hosted tools 转 Chat | 已完成 | 无 function tools 时移除 tool choice |
| Unsupported cache retention 降级 | 已完成 | 仅精确 400 同 Channel 重试一次 |
| Lease Control API | 已完成 | 只读 |
| Lease MCP 评估 | 已完成 | 只读 |
| 前端 Lease 设置 | 已完成 | 开关与 TTL |
| 前端 Session / Cache 大屏 | 未实现 | 继续延期 |
| 客户端来源聚合 | 未实现 | 原始 session_source / UA 已有 |
| Capacity scope 联动 cooldown | 未实现 | 当前只记录 |
| 价格切换渠道 | 明确暂停 | 不进入当前路线 |
| Token 花费统计扩展 | 明确暂停 | 不继续配置或展示 |
| 4B Shadow cost routing | 暂停 | 当前没有必要做 |
| 多模型 Agent 编排 | 不属于 Rotor | 交给 Coding Agent / orchestration 层 |

## 九、下一步

### P0：打 wheel 前必须完成

1. 单独修复 MindAgent system prompt 顺序测试；不要把它和 Session Lease 逻辑耦合。
2. 重新运行根项目全量测试，目标为 0 failed。
3. 再运行 MCP 全量测试。
4. 构建 `rotor-gateway 0.3.0` wheel。
5. 在干净虚拟环境安装 wheel，做启动、数据库迁移、`/health`、Chat、Anthropic 和 Responses smoke test。
6. 确认 Session Lease Alembic revision 和新增模块都进入 wheel。

### P1：使用新版本收集一轮干净数据

1. 使用 SQLite backup API 或停服副本，先保证 `quick_check=ok`。
2. 正常使用 Codex、Claude Code 和实际 Channel 至少一个完整工作日。
3. 重点验证以下不变量：
   - stable Session 的 Lease application rate 接近 100%；
   - `cached + uncached + cache_write <= prompt`；
   - DeepSeek 不再出现 `tool_choice` 但无 `tools` 的 400；
   - 每个 routing decision 都有 attempt 或可解释的 cancelled 终态；
   - failed / cancelled request 不产生 assigned / renewed / migrated；
   - migration 只来自 fallback、leased channel unavailable 或 idle expiry；
   - 同 Session 不在健康 Channel 间无故来回迁移。
4. 用 MCP 按 logical model 和精确时间窗评估，不使用全局混合流量命中率。

### P2：数据稳定后再决定的小改进

- 增加按 `session_source` / User-Agent 的客户端来源聚合；
- 视使用频率决定是否增加一个简单的 Session Lease 状态页面；
- 增加 started conversation / missing attempt 的一致性巡检；
- 观察 migration 后 cache 重建时长，调整各 Channel / model 的 Lease TTL；
- 观察 ConversationStore drop counters，只有实际发生丢弃时再决定是否处理；
- 观察流式 overload 频率，必要时调整 30 秒 cooldown。

### 明确不进入下一步

- 不做按峰谷价格主动迁移活跃 Session；
- 不做 token 花费大屏或成本优化评分；
- 不启动 4B / 4C cost routing；
- 不自动改写 prompt 或插入 cache breakpoints；
- 不做跨实际模型的自动 fallback；
- 不把 MindAgent / 多 Agent 编排塞进路由内核。

只有未来重新提出需求，且基础事实连续多个窗口完整、对照实验能证明净收益时，才重新评估这些事项。

## 十、仍需单独处理的项目级问题

这些问题不属于 Session Lease，但会影响后续发布优先级：

1. Admin API 仍缺少完整鉴权，是当前项目最高安全风险；对外暴露前必须处理。
2. SQLite 高并发写锁仍是架构上限，未来可考虑写入聚合或 PostgreSQL。
3. RequestLog 数据继续增长后可能需要 `(model, created_at)`、`(channel_id, created_at)` 复合索引。
4. RateLimitMiddleware 仍是单进程内存状态，存在 IP 字典增长和多 worker 不一致问题。
5. Responses 转换渠道尚未映射 `reasoning_content`，等待 Codex 实测是否真的影响体验。

## 十一、文件变更清单

以下是创建本文档前的完整工作树清单。它包含本轮 Session / KV Cache 工作以及同一工作树内完成的配套稳定性、可观测性、文档和测试修改。

### 新增文件（18）

```text
alembic/versions/2026_08_19_1200_f6a7b8c9d0e1_session_leases.py
docs/issues-todo-2026-08-18.md
docs/session-aware-routing-design.md
src/rotor/core/client_session.py
src/rotor/core/resource_scopes.py
src/rotor/gateway/pricing.py
src/rotor/gateway/provider_facts.py
src/rotor/models/session_lease.py
src/rotor/services/session_lease_evaluation.py
src/rotor/services/session_leases.py
tests/test_admin_channels_api.py
tests/test_client_session.py
tests/test_control_session_lease_evaluation_api.py
tests/test_database_migrations.py
tests/test_pricing.py
tests/test_provider_facts.py
tests/test_resource_scopes.py
tests/test_session_leases.py
```

本文档自身是创建后的第 19 个新增文件。

### 修改文件（74）

#### 包版本与入口

```text
pyproject.toml
src/rotor/__init__.py
src/rotor/main.py
alembic/env.py
```

#### 核心、协议、路由与 accounting

```text
src/rotor/adapters/protocol/anthropic.py
src/rotor/adapters/protocol/converter.py
src/rotor/adapters/protocol/responses.py
src/rotor/api/v1/anthropic.py
src/rotor/api/v1/chat.py
src/rotor/api/v1/images.py
src/rotor/api/v1/responses.py
src/rotor/application_settings.py
src/rotor/conversations/store.py
src/rotor/core/exceptions.py
src/rotor/core/middleware.py
src/rotor/database.py
src/rotor/gateway/accounting.py
src/rotor/gateway/routing.py
src/rotor/services/response_routes.py
```

#### Model、schema、service 与 API

```text
src/rotor/models/log.py
src/rotor/models/usage.py
src/rotor/schemas/channel.py
src/rotor/schemas/control.py
src/rotor/schemas/request.py
src/rotor/schemas/request_trace.py
src/rotor/services/channels.py
src/rotor/services/model_usage.py
src/rotor/services/request_traces.py
src/rotor/api/admin/channels.py
src/rotor/api/admin/logs.py
src/rotor/api/admin/mindagent.py
src/rotor/api/admin/settings.py
src/rotor/api/control/requests.py
```

#### 前端

```text
src/rotor/frontend/app.js
src/rotor/frontend/config.js
src/rotor/frontend/index.html
src/rotor/frontend/pages/channels.js
src/rotor/frontend/pages/overview.js
```

#### MCP

```text
mcp/README.md
mcp/src/rotor_mcp/client.py
mcp/src/rotor_mcp/schemas.py
mcp/src/rotor_mcp/server.py
mcp/tests/test_client.py
mcp/tests/test_server.py
```

#### 测试

```text
tests/test_accounting_usage.py
tests/test_adaptive_routing.py
tests/test_anthropic_protocol.py
tests/test_application_settings.py
tests/test_chat_attempts.py
tests/test_control_channels_api.py
tests/test_control_model_usage_api.py
tests/test_conversation_store.py
tests/test_fallback.py
tests/test_log_stats.py
tests/test_request_traces.py
tests/test_response_routes.py
tests/test_responses_protocol.py
tests/test_routing.py
tests/test_upstream_error_fact.py
```

#### 用户文档与 GitBook

```text
README.md
README.en.md
docs/channels.md
docs/client-integrations.md
gitbook/README.md
gitbook/concepts/accounting-and-state.md
gitbook/concepts/routing-and-fallback.md
gitbook/development/database-migrations.md
gitbook/guides/api-keys.md
gitbook/guides/routing.md
gitbook/operations/configuration.md
gitbook/operations/observability.md
gitbook/reference/admin-api.md
gitbook/reference/channel-schema.md
gitbook/reference/client-api.md
```

## 十二、相关文档

- [Session-aware Routing 设计](session-aware-routing-design.md)
- [问题清单与待办](issues-todo-2026-08-18.md)
- [客户端接入](client-integrations.md)
- [Channel 配置](channels.md)
- [MCP README](../mcp/README.md)
- [GitBook Routing Guide](../gitbook/guides/routing.md)
- [GitBook Observability](../gitbook/operations/observability.md)
