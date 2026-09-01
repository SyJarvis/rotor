# 日志、用量与数据存储

Rotor 的管理页提供渠道状态、API Key、用量和请求日志视图；相同数据也可通过
管理 API 查询。

## 健康检查

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api
```

`/health` 证明应用能够响应，但当前不是数据库或每个上游渠道的深度健康检查。
渠道连通性应使用渠道测试接口。

## 管理控制台概览

管理页首页的请求数、Token、成功率和平均延迟都是所有请求日志的最近 7 天汇总。
它不是全部历史数据，也不按单个 Channel、模型或用户 Token 过滤。

首页的“运行状态”是请求流量信号，不是上游探测：

- 最近 15 分钟没有请求时显示“空闲”；
- 有请求且全部成功时显示“系统健康”；
- 同时存在成功和失败时显示“部分降级”；
- 有请求但全部失败时显示“全部异常”。

“近期异常”列出最近 24 小时的最多 5 条失败请求，并可跳转到带失败状态筛选的
请求日志。页面上的“最近成功”显示相对时长，悬停可查看绝对时间。

因此，首页适合发现当前请求路径是否异常；它不替代 `/health`，也不表示每个上游
Channel 都已完成连通性探测。

## 请求日志

常用接口：

```text
GET /api/admin/logs
GET /api/admin/logs/count
GET /api/admin/logs/{log_id}
GET /api/admin/logs/stats
GET /api/admin/logs/timeseries
GET /api/admin/logs/timeseries_by_model
GET /api/admin/logs/models
```

日志列表和统计接口可以按模型、Channel、成功状态和时间过滤。用量与日志页面支持
`period=day|week|month` 和 `period_date=YYYY-MM-DD`，例如：

```text
GET /api/admin/logs/stats?period=day&period_date=2026-08-17
GET /api/admin/logs?period=week&period_date=2026-08-17&success=false
```

自然日、周和月的边界使用运行时 `display_timezone` 设置计算，默认是
`Asia/Shanghai`。请求日志表格以该时区显示绝对时间，格式为
`YYYY-MM-DD HH:mm`。需要任意时间窗口时，对汇总接口同时传入
`start_time` 和 `end_time`；不要将它们与 `period` 混用。

详情可能包含经过清理的上游错误响应；仍应只向管理员开放。

## Cache Usage 口径

2026-08-19 起，新记账记录使用 usage schema v2，并区分：

| 字段 | 含义 |
| --- | --- |
| `prompt_tokens` | 该请求真实处理的总输入 Token |
| `uncached_input_tokens` | 未从缓存读取、也未写入缓存的输入 Token |
| `cached_tokens` | cache read Token；保留旧字段名以兼容现有 API |
| `cache_write_tokens` | 本次写入缓存的 Token |
| `cache_write_5m_tokens` | 可确认使用 5 分钟 TTL 的 cache write Token |
| `cache_write_1h_tokens` | 可确认使用 1 小时 TTL 的 cache write Token |

Anthropic 的 `input_tokens` 只是未缓存部分，Rotor 会加上 cache read 和 cache write
得到 `prompt_tokens`；DeepSeek 使用 hit/miss；OpenAI 的 read/write 是
`prompt_tokens` 的子集，不重复相加。

数据库启动时会为旧表幂等补列。历史记录标记为 usage schema v1，新记录为 v2；
因为旧 Anthropic 记录已经丢失 cache write 信息，Rotor 不猜测回填。聚合结果中的
`usage_v2_requests` / `usage_v2_ledger_count` 可用于判断新口径覆盖率。

## Capacity 响应事实

Rotor 会从成功和失败的上游响应中保存所有名称包含 `ratelimit` 的 Header，以及
`Retry-After`，并将它们写入 `capacity_snapshot`。这能保留 provider 返回的 remaining、
reset、limit 等原始事实，同时避免把 Authorization、Cookie 或普通响应 Header 写入
用量表。

这些值目前只用于日志与 request trace 观测，不参与候选排序或 cooldown 计算。
`capacity_snapshot` 本身不能说明多个 Channel 是否共享同一个账号限额；这个关系
只能由管理员通过 `capacity_scope` 显式声明。

新请求会同时保存生效的 `cache_scope`、`capacity_scope` 和 `billing_scope`。未配置
时均为 `channel:<id>`；相同类型、相同显式 ID 才表示跨 Channel 共享。历史记录的
scope 保持请求发生时的快照，不随之后的 Channel 配置变化。

## Tariff 与费用口径

配置了 Channel `extra.tariffs` 后，成功请求会按实际成功上游 attempt 的开始时刻
匹配费率，并记录：

- `input_cost`、`output_cost` 和 `total_cost`；
- `currency`、`tariff_version` 和 `tariff_period`；
- 实际匹配的 `tariff_snapshot`，单位为每百万 Token；
- `cost_status`：`calculated`、`unknown`、`invalid_tariff`、
  `incomplete_tariff` 或 `not_applicable`。

`unknown` 不等于免费；未配置 tariff 或上游未返回 usage 都保持 `unknown`。只有
`calculated` 记录才进入费用聚合。不同币种通过
`cost_totals_by_currency` 分开返回；只有结果中恰好存在一个币种时，兼容字段
`total_cost` 和 `currency` 才有单一含义，混合币种不会被直接相加。

费用是依据管理员显式配置的 tariff 得出的可审计计算值，不是供应商最终账单。
当前路由器不会使用这些费用改变渠道选择。

## Session Lease 事实

`session_leases` 保存当前权威绑定，唯一键为 Rotor Token、Session ID 和 logical model。
`session_lease_events` 追加记录 `assigned`、`renewed`、`migrated`、`expired`，以及旧/新
Channel 和原因。事件通过 `request_id` 出现在 Control API request trace 的
`session_lease_events` 中；routing decision 的 feature snapshot 同时说明本次是否读到
并采用租约。

租约只在上游成功后变更，失败 attempt 不会污染绑定。流式请求在流完成后提交；queued
Responses 在上游已接受并建立对象归属时提交。当前没有自动清理事件表的保留策略。

### Session Lease 评估窗口

只读 Control API 提供聚合接口：

```text
GET /api/control/v1/usage/session-leases
GET /api/control/v1/usage/session-leases?model=gpt-5.4&start_time=2026-08-18T00:00:00Z&end_time=2026-08-19T00:00:00Z
```

接口需要 `usage:read`，默认窗口为最近 24 小时，最大 30 天，结束时间不包含。它不会
返回 Session ID，只汇总以下事实：

- routing decision 中稳定 Session 的覆盖率，以及首选租约实际被采用的比例；
- assigned、renewed、migrated、expired 事件、迁移率和 fallback 迁移次数；
- provider usage、usage schema v2 和可计算费用的覆盖率；
- uncached input、cache read、cache write 的 Token 数与比例；
- fallback 请求、fallback 成功率、429 attempt 和 5xx attempt；
- 按 `channel_id × tariff_version × tariff_period × currency` 分组的缓存与费用 cohort。

`facts_complete_for_evaluation` 是数据门槛，不是收益结论。以下任一情况会出现在
`blocking_reasons`：没有路由决策、没有稳定 Session、没有 Lease 事件、没有成功
usage、provider usage / v2 / cost 覆盖不完整，或窗口内存在多个币种。跨币种费用不会
相加；应优先按 logical model 查询，再缩小到同币种、同费率条件可比较的窗口。

即使没有 blocking reason，也只能说明可以开始比较。是否优于简单 Session Lease，
仍需在多个窗口中观察迁移、缓存、费用、429/5xx 和 fallback，并使用 shadow 或受控
实验验证；Rotor 不会根据该接口自动改变路由设置。

## 路由响应头

成功的代理响应可以包含：

| Header | 含义 |
| --- | --- |
| `X-Rotor-Channel` | 实际使用的 Channel 名称 |
| `X-Rotor-Provider-Model` | 映射后发送给上游的模型 |
| `X-Rotor-Fallback` | 使用了非首选候选渠道时为 `true` |

这些响应头适合定位模型映射和故障转移，但不替代数据库中的完整路由决策。

## 持久化位置

默认情况下：

```text
~/.cache/rotor/rotor.db
~/.cache/rotor/conversations/
~/.cache/rotor/logs/YYYY-MM/YYYY-MM-DD.log
~/.rotor/settings.json
```

SQLite 启用 WAL、5 秒 busy timeout 和 `synchronous=NORMAL`，以减少请求记账和
会话 worker 并发写入时的锁冲突。
运行日志同时输出到控制台和本地文件；本地文件按月创建目录，每个自然日一个
`.log` 文件，日期使用 Rotor 进程的本地时区。

## 数据保留

当前实现没有自动清理 request logs、usage ledger、routing decisions、Session Lease
events、response routes、会话文件或按日运行日志的保留策略。运维方应根据容量和隐私要求
安排备份、归档与删除，并确保删除操作不会破坏仍在使用的原生 Responses 对象。
