# DB 读写性能瓶颈分析与 P0 优化记录

初始记录：2026-09-04；更新：2026-09-09。
状态：保留初始分析与历史验证记录；当前实现修正见第 8、9 节。

## 1. 背景

压测发现性能瓶颈，怀疑是 DB 读写问题。经盘点确认：单次请求在关键路径上做了太多同步写事务，
默认引擎无池化调优，SQLite 单写者 + 单 Token 热点行一压就串行。

## 2. 优化前单请求 DB 操作盘点（非流式成功，以 `chat_completions` 为例）

| 阶段 | 操作 | 位置 |
|---|---|---|
| 鉴权 | `SELECT Token WHERE key` | `src/rotor/core/deps.py:get_current_token` |
| 选路 | `SELECT Channel WHERE enabled` 全表 + Python 过滤 | `src/rotor/core/deps.py:get_available_channels` |
| 亲和 | `SELECT SessionLease` | `src/rotor/services/session_leases.py:get_preferred_channel_id` |
| 决策 | `INSERT routing_decisions` + 上游调用前 `commit` | `src/rotor/api/v1/chat.py`（已改） |
| 单次 attempt | 独立 session `INSERT request_attempts` + 独立 `commit` | `src/rotor/gateway/attempts.py`（已改） |
| 记账 | `UPDATE tokens` + `INSERT request_logs` + `INSERT usage_ledger` + lease 更新 + `commit` | `src/rotor/gateway/accounting.py:record_success` |
| 会话存档 | 后台单 worker：upsert + update + update，各一次 `commit` | `src/rotor/conversations/store.py` |

优化前成功一次 ≈ **6 个写事务、7+ 行插入、2 行热点更新**，
横跨 `routing_decisions / request_attempts / request_logs / usage_ledger / session_leases(+events) / conversation_records / tokens`。
流式最终记账使用另一个短 session；session 数量不能直接换算为同时占用的连接数量。

## 3. 初始瓶颈假设与代码观察（尚非性能归因）

1. **关键路径上有同步提交**：`record_routing_decision` 后立刻 `commit`，TTFT 里多一次写往返；
   失败重试每个 attempt 各有一次 `record_failure + commit`。
2. **单 Token 行热点**：每次成功都 `UPDATE tokens`（计数/quota/last_used），单 key 压测下所有并发争同一行。
3. **固定会话下 Lease 热点**：固定 `X-Conversation-Id` 时每次成功都 `UPDATE session_leases` 同一行 + `INSERT session_lease_events`。
4. **Attempt 独立事务**：本可并入主记账事务，非流式却单独 session + commit。
5. **双写放大**：`RequestLog` + `UsageLedger` 存几乎相同的 token/cost 字段，一次请求两次大行 INSERT（含 JSON 快照）。
6. **连接池需实测**（`src/rotor/database.py`）：没有显式设置 `pool_size/max_overflow/pool_timeout/pool_pre_ping`；
   实际连接占用取决于事务生命周期，不能按 request/attempt/worker 的 session 数量推算。
7. **读无缓存**：Token、Channel 几乎不变却每请求全量查。
8. **后台单 worker 串行提交（优化前）**：每 event 一 `commit`，每请求 3 个 event。
   worker 常驻 session 不等于永久占用连接；事务结束后连接可归还池中。
9. **`get_db` 无条件 commit**：无脏数据也走一次 `commit()`。
10. **Admin 聚合查询重**：stats/timeseries/model_usage/recent_failures 全表 `SUM/COUNT/GROUP BY`，缺复合索引。

## 4. P0 改动（已落地）

目标：合并非流式成功路径的前台提交。一个主记账事务不等于整个请求只有一个事务；
后台存档、流式决策提交和失败重试必须另计，历史行数统计不足以验证总提交次数。

### 4.1 上游调用前不再 commit（四端点）

- `src/rotor/api/v1/chat.py`、`anthropic.py`、`images.py`、`responses.py`：
  非流式主路径暂存 `record_routing_decision`，随最终记账提交；后台存档仍可在等待上游期间写库。
- 流式：routing decision 是不可变审计行，在 `_handle_streaming_request` 入口 `db.commit()`
  后 `db.close()` 释放连接（`close()` 会回滚未提交行，所以必须 commit 而非 flush；
  这是 benchmark 实测确认的：flush 后 close 会丢行）。
  最终记账（attempt + usage + token）在短 session 里一次提交。`commit` 加 `hasattr` 保护以兼容测试 double。

### 4.2 Attempt 并入主事务

- `src/rotor/gateway/attempts.py`：新增 `build_attempt()` + `record(..., db=)` 参数。
  非流式成功/失败、流式成功/失败的 attempt 都与记账同事务，用 `begin_nested()` savepoint 隔离唯一键冲突
  （重复 attempt 只回滚自己，不丢 routing/usage 行；无 `begin_nested` 的测试 double 回退到 add+flush）。
- 各端点调用处传入 `db=db` / `db=stream_db`；取消/独立路径保留原独立 session 逻辑。

### 4.3 `get_db` 不再无条件 commit

- `src/rotor/database.py`：正常退出时仍有事务则提交，包括已 flush 的 ORM 写入与 Core DML；
  handler 已显式提交且未开启新事务时跳过提交。异常时回滚，最终关闭 session；保留取消安全清理。

### 4.4 Token 热点缓解

- `src/rotor/gateway/accounting.py:_update_token_counters`：
  计数通过数据库原子累加，与账本同事务；禁用判断使用数据库当前额度，保留已禁用状态。
  `last_used_at` 仅 None 或 ≥60s 才刷新。这保证已记账用量累计，不提供并发请求执行前的硬额度预留。

### 4.5 Lease 同 channel 命中省略续期事件

- `src/rotor/services/session_leases.py:record_session_lease_success`：
  同 channel 命中（`renewed`）更新 `last_used_at` 和 `expires_at`，不 INSERT event，返回 `("renewed",)`。
  assign/migrate/expire 路径不变；每次成功请求都推进空闲到期时间。
- `tests/test_session_leases.py` 两个事件序列断言已同步更新。

## 5. 历史验证记录（2026-09-04）

- `tests/` 全量：**389 passed**（含 lease 并发测试）。
- SQLite 真库验证：routing_decision + attempt + request_log + usage_ledger 单事务各 1 行。
- Fake session 验证：非流式成功 `commits == 1`，四表行齐全。

## 6. 历史 Benchmark（2026-09-04，旧脚本）

以下数值仅保留历史，**不作为当前性能基准或优化收益证据**。旧脚本使用进程内 ASGI transport，
流式响应可能被缓冲，因此 TTFT 不可靠；表行数变化不是 INSERT/UPDATE/commit 次数；
取样前未明确排空后台 worker，也未核对 Token 累计值与账本总量。

| 场景 | 成功率 | RPS | latency p50 / p95 | TTFT p50 / p95 | DB 写/请求 |
|---|---|---|---|---|---|
| chat 非流式 c8×100 | 100/100 | ~145 | ~7ms / ~126ms | — | routing/attempt/log/ledger 各 1.00，lease 行数无增长（不代表无 UPDATE） |
| chat 流式 c8×100 | 100/100 | ~133 | ~13ms / ~196ms | ~13ms / ~196ms | 同上（decision 在流开始前 commit，attempt+usage 短 session 单 commit） |

旧数据不能区分连接等待、写锁、提交和后台工作对延迟的贡献，也不能据此认定迁移数据库或加索引的收益。
当前脚本已校准统计口径；本次对比结果见第 10 节。

benchmark 脚本：`scripts/bench_gateway.py`（复用 `benchmarks/runner.py` 的 `run_requests` +
标准 artifact 输出到 `benchmarks/results/bench-<stamp>/`）。注意它 patch 了各端点模块的
`async_session_maker` 引用（`from ... import` 绑定在 import 时固定，只 patch
`rotor.database` 不够）以及端点模块的 `AsyncClient`（指向上游 mock transport）。

## 7. P1 / P2（可选，暂缓）

- P1（高并发再做，有一致性/语义风险）：引擎池化参数 env 可配、Token/Channel 短 TTL 读缓存。
  conversation worker 的有限批量提交已在第 9 节落地。
- P2（按需）：`(created_at, model)` 类复合索引、Admin 大查询窗口上限与超时；生产切 Postgres。

## 8. 2026-09-09：修复 Lease 空闲续期

此前省略同 channel 的 UPDATE 会让持续活跃的会话仍按首次分配时间过期，
不符合 idle-TTL 语义。现恢复成功请求对 `last_used_at` 和 `expires_at` 的更新，
保留不插入 `renewed` 事件的优化。SQLite 回归覆盖 15 分钟 TTL 下第 14 分钟续期、
第 16 分钟仍命中、最后活动超过 TTL 后失效，并通过新 session 读取确认续期持久化。

默认空闲 TTL 延长为 30 分钟（1800 秒），模型与 channel 的显式覆盖仍优先。
已有 `~/.rotor/settings.json` 中保存的 TTL 不自动覆盖；已有部署如需延长，
应在管理设置中将该值调整为 1800 秒。

## 9. 2026-09-09：事务清理与后台有限批量提交

- `get_db` 改为依据 `in_transaction()` 提交，修复 ORM flush 后脏集合为空、Core DML 无脏对象时写入被回滚的问题。
  SQLite 回归覆盖正常提交、显式提交不重复、异常回滚以及取消期间完成清理。
- `create_database_engine()` 供生产与 benchmark 共用，保留原连接池参数；SQLite 统一使用
  WAL、`busy_timeout=5000`、`synchronous=NORMAL`，已检查两个独立连接的 PRAGMA。
- ConversationStore 只合并队列中连续的 `db_only` 事件，每批最多 32 条、一次提交；
  遇到文件事件或停止信号即结束当前批次，文件事件保持独立处理。
- 批失败先回滚，再按原顺序逐项重试；已确认成功追加的文件用事件状态标记，数据库失败后的重试不重复追加。
  该标记只存在于进程内，**不保证文件与数据库跨崩溃原子性**，也不解决文件部分写入后 I/O 报错的不确定性。
- `drain()` 等待队列和正在处理的事件（含重试）完成，并报告超时或 worker 异常；
  队列为空本身不足以证明数据库提交完成。基准取样须在 drain 后检查累计值及丢弃计数。

相关验证位于 `tests/test_database_session.py`、`tests/test_accounting_transactions.py` 和
`tests/test_conversation_store.py`；本节记录实现与正确性边界，不宣称吞吐或延迟收益。


## 10. 2026-09-09：校准后的后台提交对比

同机各运行 3 轮 chat 非流式基准：并发 8、测量 100 请求、预热 5 请求、进程内 transport。
前后使用同一份新基准脚本、当前 `get_db` 和共享引擎配置，只替换 ConversationStore：
优化前加载修改前保存的 worker，优化后使用有限批量提交。各轮 SQLite 均为独立临时数据库，
确认 WAL、`busy_timeout=5000`、`synchronous=NORMAL`；预热后和测量后均排空后台工作再取样。

| 指标（每轮 100 请求） | 优化前（3 轮） | 优化后（3 轮） |
|---|---|---|
| Engine commit 尝试次数 | 400 / 400 / 400 | 293 / 292 / 292 |
| RPS | 150.9 / 155.5 / 145.3 | 157.5 / 168.3 / 163.9 |
| 请求延迟 p95（ms） | 267.3 / 146.1 / 192.3 | 213.9 / 156.2 / 202.3 |
| 成功请求 | 每轮 100 | 每轮 100 |

Engine commit 尝试次数减少约 **27%**。SQL 操作数保持一致：每轮 SELECT 400、INSERT 500、
UPDATE 400、SAVEPOINT 100、RELEASE 100，说明本次主要减少事务提交，并未减少 SQL 语句。
这些指标来自 engine 事件，记录尝试次数；连接持有时间也由 checkout/checkin 统计，不能当成连接池等待时间。

六轮的账本、请求日志、Token 请求累计以及完成的会话索引均增加 100 条/次，Token 用量、
额度累计、账本及日志用量均增加 1,100 tokens；JSONL 均增加 100 个唯一请求，完整性检查无错误。
统计不含 5 次预热请求，DB 与存档统计包含响应完成后排空的后台工作；响应 RPS 和延迟本身不包含额外排空耗时，
后者单独记录为 `post_response_drain_seconds`。

RPS 中位数从 150.9 到 163.9，是同机短压测观察，不能外推生产收益。p95 在第 2、3 轮反而上升，
本次不承诺尾延迟改善，也未完成 SQLite 锁等待或连接池等待的性能归因。
本次本地原始证据目录为 `/tmp/rotor-batch-comparison-ixgok6ol/`，包含 `comparison.json` 及
`before-1..3`、`after-1..3` 的 `summary.json`、`db-metrics.json`；该临时目录不随仓库分发。

复现当前实现的同规模基准：

```bash
PYTHONPATH=src .venv/bin/python scripts/bench_gateway.py \
  --protocol chat --no-stream --transport in-process \
  --concurrency 8 --requests 100 --warmup 5 \
  --output-dir /tmp/rotor-bench-chat
```

测量流式首字延迟时使用 `--stream --transport http`，例如：

```bash
PYTHONPATH=src .venv/bin/python scripts/bench_gateway.py \
  --protocol chat --stream --transport http \
  --concurrency 8 --requests 100 --warmup 5 \
  --chunk-count 3 --chunk-interval 0.02 \
  --output-dir /tmp/rotor-bench-chat-http
```

HTTP 模式启动本地网关与模拟上游，经真实 HTTP 连接采样；它仍是隔离模拟环境。
进程内模式不报告 TTFT，避免将 ASGI 缓冲时间误当作网络首字延迟。

## 11. 2026-09-09：Anthropic 流式结束事件与记账顺序

校准真实 HTTP 压测时发现，Anthropic 的 `message_stop` 原先先于最终记账发送；客户端收到结束事件后立即关闭连接，可能取消尚未完成的记账。`src/rotor/api/v1/anthropic.py:_handle_streaming_request` 已对原生和转换流暂存该事件，等待成功记账事务提交、会话存档事件入队后再发送，记账失败则输出错误事件。这里保证存档已入队，不表示后台文件已落盘。回归 `tests/test_anthropic_protocol.py:test_message_stop_is_sent_after_accounting_commit_and_archive_enqueue` 覆盖两类流的成功和记账失败路径，并在读取结束事件时使用独立数据库 session 核对账本、计数与 attempt，以及存档队列状态。
