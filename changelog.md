# Changelog

## 2026-09-11 — Rotor 0.5.1

### 协议完整性与错误外壳

- 新增 [上游完整性校验](src/rotor/adapters/protocol/openai_integrity.py) 与 [Anthropic 完整性校验](src/rotor/adapters/protocol/anthropic_integrity.py)：在返回结果与流式事件上校验完成状态、截断与业务错误，替代原先仅按 HTTP 状态判断的做法。
- 新增 [协议错误外壳](src/rotor/core/anthropic_errors.py)，[应用入口](src/rotor/main.py) 注册对应异常处理器；错误响应不再泄漏校验输入。
- 新增 [OpenAI 协议错误外壳](src/rotor/core/openai_errors.py)：OpenAI 兼容调用失败时返回结构化错误，保留上游状态码、请求 ID 与脱敏响应体，并按类映射状态；不再统一降级为 500。管理网页聊天展示同一份上游诊断信息，回归见 [test_openai_error_envelope.py](tests/test_openai_error_envelope.py)、[test_mindagent_chat.py](tests/test_mindagent_chat.py)。
- [异常归一](src/rotor/core/exceptions.py) 补充 `Retry-After` 解析与失败分类，回归见 [test_retry_after.py](tests/test_retry_after.py)、[test_openai_response_integrity.py](tests/test_openai_response_integrity.py)、[test_anthropic_response_integrity.py](tests/test_anthropic_response_integrity.py)。

### 路由能力约束与尝试准入

- 新增 [请求能力判定](src/rotor/gateway/capabilities.py)，[路由引擎](src/rotor/gateway/routing.py) 增加尝试准入、冷却期与恢复探针：并发候选只准入一个恢复请求，冷却期内不发送。
- 新增 [流式准入响应](src/rotor/gateway/streaming.py)，在上游响应完整生命周期内释放 attempt 归属；未开始消费、发送失败、断连与取消均释放。
- 回归见 [test_routing.py](tests/test_routing.py)、[test_request_capabilities.py](tests/test_request_capabilities.py)、[test_recovery_admission.py](tests/test_recovery_admission.py)、[test_admission_streaming.py](tests/test_admission_streaming.py)、[test_attempt_latency.py](tests/test_attempt_latency.py)。

### 会话租约

- [会话租约](src/rotor/services/session_leases.py) 支持到期复评与失败延期，新增 [复评服务](src/rotor/services/session_lease_evaluation.py)；关闭开关或来源缺失时回退既有行为。
- 回归见 [test_session_leases.py](tests/test_session_leases.py)、[test_lease_reassessment.py](tests/test_lease_reassessment.py)、[test_lease_reassessment_endpoints.py](tests/test_lease_reassessment_endpoints.py)。

### 观测与监控

- 新增 [进程内性能观测](src/rotor/observability.py)：数据库连接获取、SQL、提交与连接持有耗时，协议成功/失败/取消分类，请求耗时与首字延迟，均为有界内存样本，重启清零。
- 新增 [超大事件解析器](src/rotor/core/_response_event.py)，在 64 KiB 上限内识别完成、失败与不完整终态，修复较长输出被误判为失败的问题。
- 新增 [管理监控 API](src/rotor/api/admin/monitoring.py) 与管理网页「监控 → 性能 / 会话来源」页面（[performance.js](src/rotor/frontend/pages/performance.js)、[monitoring.js](src/rotor/frontend/pages/monitoring.js)）。
- 回归见 [test_gateway_metrics.py](tests/test_gateway_metrics.py)、[test_database_metrics.py](tests/test_database_metrics.py)、[test_performance_monitoring.py](tests/test_performance_monitoring.py)、[test_admin_monitoring.py](tests/test_admin_monitoring.py)。
- 性能计数属于单进程，不能视为全局汇总。

### 数据库与记账

- [会话与引擎](src/rotor/database.py) 统一引擎工厂并补齐事务收尾：正常退出仍有事务时提交，异常回滚，取消期间完成清理。
- [记账服务](src/rotor/gateway/accounting.py) 使用数据库内原子累加，与账本同事务；额度禁用判断使用数据库当前值。
- [归档 worker](src/rotor/conversations/store.py) 合并连续数据库事件按批提交，每批最多 32 条，不跨文件事件合批、不改变顺序。
- 新增 [空请求维护](src/rotor/maintenance/empty_requests.py) 与 `rotor` CLI 入口；默认只读，执行清理需 `apply` 与显式 `confirm`，删除前先归档。
- 回归见 [test_accounting_transactions.py](tests/test_accounting_transactions.py)、[test_database_session.py](tests/test_database_session.py)、[test_database_migrations.py](tests/test_database_migrations.py)、[test_conversation_store.py](tests/test_conversation_store.py)、[test_completion_terminal_accounting.py](tests/test_completion_terminal_accounting.py)、[test_empty_request_cleanup.py](tests/test_empty_request_cleanup.py)。

### 客户端来源与渠道目录

- [会话身份解析](src/rotor/core/client_session.py) 新增客户端来源识别，支持请求头 `x-mindcode-session-id`；显式 `x-conversation-id`、`x-rotor-session-id` 优先级更高。
- 新增 [Anthropic Models 目录](src/rotor/api/v1/anthropic_models.py)，[渠道管理](src/rotor/api/admin/channels.py) 增加草稿探测不写配置、上游分页完整性与目录标签；[渠道预设](src/rotor/channels/presets.py) 增加协议→路径拼接。
- 回归见 [test_client_session.py](tests/test_client_session.py)、[test_channel_model_catalog.py](tests/test_channel_model_catalog.py)、[test_anthropic_models.py](tests/test_anthropic_models.py)、[test_anthropic_urls.py](tests/test_anthropic_urls.py) 与前端测试 [test_channel_form_frontend.mjs](tests/test_channel_form_frontend.mjs)、[test_channel_catalog_label_frontend.mjs](tests/test_channel_catalog_label_frontend.mjs)、[test_client_sources_frontend.mjs](tests/test_client_sources_frontend.mjs)、[test_monitoring_frontend.mjs](tests/test_monitoring_frontend.mjs)。

### 基准测试

- 新增 [benchmarks](benchmarks) 框架与 [bench_gateway.py](scripts/bench_gateway.py)：模拟上游、客户端与资源采样，输出环境、原始结果、汇总与报告。运行产物目录 `benchmarks/results/` 不入库。

### 版本与前端缓存

- 版本提升至 `0.5.1`（[pyproject.toml](pyproject.toml)、[__init__.py](src/rotor/__init__.py)）；容器基础镜像改为 `python:3.12-slim`。
- 递增入口缓存版本：`index.html` 的 `config.js?v=30`、`auth.js?v=11`；`app.js` 动态加载的 `monitoring.js?v=6`。

### 验证

- 全量测试：`pytest -q` 1498 passed、6 subtests passed、13 项既有弃用警告。
- 前端测试：`node --experimental-vm-modules` 运行 4 个 `tests/*.mjs`，14 passed、0 fail。
- `git diff --check` 通过。

## 2026-09-10 — 协议亲和路由（同协议优先）

### 新增

- [RoutingEngine](src/rotor/gateway/routing.py) 新增 `protocol_family()` 与 `protocol_affinity_enabled` 开关：在能力过滤与策略排序之后，将候选渠道稳定分区为「与请求同协议族（openai_chat/openai_responses/anthropic_messages）」和「需协议转换」两组，同族渠道始终排在前面，组内保持原策略顺序。协议仅作软分层，不替代 priority/weight/adaptive；`responses_native` 硬过滤、session lease 置顶、cooldown 回退的优先级均高于协议亲和。
- [RoutingSettings](src/rotor/application_settings.py) 新增 `routing.protocol_affinity_enabled`（默认开启），可关闭回退到旧行为。
- 回归测试：[test_routing.py](tests/test_routing.py) 新增 8 个用例（同协议跨优先级、四种策略、lease 优先、开关关闭、硬过滤优先、protocol_family 别名）。

## 2026-09-02 — Anthropic→Responses 缓存断点兼容与管理前端缓存

### 修复

- [Responses 适配器](src/rotor/adapters/protocol/responses.py) 现在识别上游以 HTTP 400、`invalid_parameter` 表示不支持 `prompt_cache_breakpoint` 的响应；在同一 Channel 上对深拷贝请求递归移除断点字段后重试一次，保留其余可用的缓存提示。该兼容重试有界，不是通用的同渠道重试循环。
- Anthropic 入站请求转换到 Responses 时产生的显式缓存断点因此可在不支持该字段的模型上回退为无断点请求；相关行为由 [Responses 协议测试](tests/test_responses_protocol.py) 覆盖。

### 管理前端

- 递增入口缓存版本：`index.html` 的 `config.js?v=27`、`auth.js?v=8`，以及 `auth.js` 动态加载的 `app.js?v=29`，使浏览器获取本次发布的管理前端脚本。未修改 Python 包版本。

## 2026-09-02 — Codex Responses 路由与空流处理

### 故障事实

- 本机实测 Channel 7 的上游支持 `/v1/responses`，但 Rotor 中的渠道元数据仍为 `openai` 协议和 `/chat/completions` 路径。Codex 请求携带原生 `reasoning` 或非空 `include` 时需要 `responses_native` 能力，因而在路由阶段返回 503；同一渠道的 OpenCode Chat 请求可用。
- 10:40 出现的 `EmptyUpstreamResponse` 属于另一条流式路径：上游以成功状态建立连接，但没有产生可计费或可下发的有效输出。

### 修复

- 部署侧将该渠道改为 `openai_responses` 协议和 `/responses` 路径，使原生 Responses 请求可被正确路由。
- [Responses 协议转换与能力判定](src/rotor/adapters/protocol/responses.py) 补齐 text/refusal、URL 图片和 function tools 等可表达子集；状态、reasoning、非空 include、原生 item 及不可表达的 tool_choice 继续要求原生 Responses，避免这些字段静默降级。
- [Chat 流式处理](src/rotor/api/v1/chat.py) 对非原生流增加同渠道单次透明重试：仅在尚未产生 meaningful output 时重试；第二次仍为空则失败，已有输出或原生 Responses 流不会重放。
- Responses 流现在能识别 canonical 顶层 `error` 和 `response.failed`，并保留 provider error code 供既有错误分类与回退逻辑使用。

相关回归测试见 [Responses 协议测试](tests/test_responses_protocol.py)、[路由测试](tests/test_routing.py) 和 [Chat 流式尝试测试](tests/test_chat_attempts.py)。

### 验证

- 全量测试：368 passed、4 subtests passed；13 项为既有弃用警告。
- `python -m compileall -q src tests` 与 `git diff --check` 均通过。
- 本机运行验证中，`/health` 返回 200；`/v1/responses` 与 `/v1/chat/completions` 的流式请求均返回 200 且产生有效输出。
- 本机 `request_attempts` 运行记录确认上述 Responses 与 Chat 请求成功完成。

### 边界

- Channel 7 的协议与路径属于部署数据库配置，不随源码 Git 传播；其他部署需要独立校正渠道元数据。
- 原生 state、reasoning 和其他不可安全转换的 Responses 语义不会做有损 Chat 降级。
- 空流重试最多额外发起一次上游请求；首次产生任何 meaningful output 后不会重试或重放。
