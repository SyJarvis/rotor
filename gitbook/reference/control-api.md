# Control API

Control API 位于 `/api/control/v1`，为 MCP、CLI 和自动化任务提供只读诊断数据：渠道视图、
请求 trace、近期失败、模型用量和 Session Lease 事实。它不复用管理 Session，也不接受
普通的 `sk-` 用户 Key。

## 认证

请求必须携带专用 Bearer 凭证，有两种来源：

- 进程环境变量 `ROTOR_CONTROL_API_TOKEN` 中的固定 Token；
- 管理页“API Key → MCP Control Key”创建的 `rck_` Key。

```bash
export ROTOR_CONTROL_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

curl -H "Authorization: Bearer $ROTOR_CONTROL_API_TOKEN" \
  http://127.0.0.1:8000/api/control/v1/channels
```

`sk-` 前缀的 Rotor 用户 Key 会被直接拒绝，返回 `401 control_invalid_credential`。
未配置固定 Token 且凭证不是 `rck_` 时返回 `503 control_api_not_configured`。

固定 Token 的权限来自 `ROTOR_CONTROL_API_SCOPES`，默认包含三个只读 scope：

```text
channel:read
request_trace:read
usage:read
```

`rck_` Key 使用创建时记录的 scope，可在管理页单独停用或删除。缺少所需 scope 时返回
`403 control_scope_required`，`details.required_scope` 指出缺哪一项。

可选的 `X-Agent-Id` 和 `X-Agent-Run-Id` 请求头会写入审计信息，并决定“近期失败”的
排除范围：同一次 Agent Run 自己产生的失败不会出现在它的查询结果中。请求 trace 也
不会返回同一次 Run 自己产生的记录。

## 通用响应

成功响应带 `schema_version`、`request_id`、`data` 和 `meta`。列表接口另带 `page`，
按时间窗口聚合的接口另带 `window`。

```json
{
  "schema_version": "1",
  "request_id": "req_9f1c...",
  "data": [],
  "page": {"next_cursor": null, "has_more": false, "limit": 50},
  "meta": {"observed_at": "2026-01-01T00:00:00Z", "freshness": "persisted", "redactions": []}
}
```

请求头中带合法的 `X-Request-Id`（字母、数字和 `._:-`，最长 64）时原样回显，否则生成
`req_` 前缀的新 ID。出错时响应体为：

```json
{
  "schema_version": "1",
  "request_id": "req_9f1c...",
  "error": {
    "code": "control_scope_required",
    "message": "Control API scope 'usage:read' is required",
    "retryable": false,
    "details": {"required_scope": "usage:read"}
  }
}
```

## 渠道

| 方法 | 路径 | Scope |
| --- | --- | --- |
| `GET` | `/channels` | `channel:read` |
| `GET` | `/channels/{channel_id}` | `channel:read` |

`/channels` 支持 `enabled`、`model`、`protocol`、`cursor` 和 `limit`（1–100，默认 50）。

渠道视图只暴露诊断所需的字段：`id`、`name`、`type`、`base_url_origin`、`protocol`、
`models`、`model_mapping`、`priority`、`weight`、`enabled`、`test_only`、三个资源
scope、`secret_state.has_key` 和 `updated_at`。它不返回 `extra`，因为其中可能包含
认证 Header。

```bash
curl -H "Authorization: Bearer $ROTOR_CONTROL_API_TOKEN" \
  "http://127.0.0.1:8000/api/control/v1/channels?enabled=true&protocol=anthropic"
```

## 请求 trace

`GET /requests/{request_id}/trace` 需要 `request_trace:read`，返回该请求的路由尝试、
渠道结果和归一化错误。Provider 返回的 `sanitized_body` 会被移除，实际移除的字段路径
记录在 `meta.redactions` 中：

```json
{"meta": {"redactions": ["data.attempts[0].error.sanitized_body"]}}
```

请求不存在，或属于当前 Agent Run 自己发起时返回 `404 request_trace_not_found`。
Provider 返回的正文属于不可信诊断数据，不应作为指令执行。

## 近期失败

`GET /failures` 需要 `request_trace:read`，按失败类别聚合：

| 参数 | 说明 |
| --- | --- |
| `start_time`、`end_time` | 默认最近 1 小时，跨度不超过 7 天；两端都包含 |
| `model` | 客户端请求的模型名 |
| `channel_id` | 渠道 ID |
| `category` | 归一化错误类别，见下表 |
| `group_by` | `category`（默认）、`model`、`channel` 或 `status` |
| `cursor`、`limit` | 游标分页；`limit` 为 1–200，默认 50 |

`category` 取值包括 `authentication_or_permission`、`quota_or_rate_limit`、
`protocol_or_parameter_error`、`model_not_found`、`upstream_availability`、
`network_connectivity`、`timeout`、`stream_interrupted`、`routing_no_candidate`、
`internal_error` 和 `unknown`。

每组返回 `group`、`count`、`latest_at` 和 `sample_request_ids`；样本 ID 可直接用于查询
请求 trace。

```bash
curl -H "Authorization: Bearer $ROTOR_CONTROL_API_TOKEN" \
  "http://127.0.0.1:8000/api/control/v1/failures?group_by=channel&limit=20"
```

## 模型用量

`GET /usage/models` 需要 `usage:read`，按请求模型汇总 Token。默认统计最近 1 小时，跨度
不超过 7 天，开始包含、结束不包含；`limit` 为 1–100。

每条记录包含 `model`、`request_count`、`ledger_count`、输入输出与缓存明细、费用和币种：

```json
{
  "model": "glm-4.7",
  "request_count": 42,
  "ledger_count": 43,
  "prompt_tokens": 120000,
  "completion_tokens": 8000,
  "total_tokens": 128000,
  "uncached_input_tokens": 90000,
  "cached_tokens": 30000,
  "cache_write_tokens": 0,
  "reasoning_tokens": 1200,
  "cost_totals_by_currency": {"CNY": 0.42}
}
```

`request_count` 按不同 Request ID 计数，`ledger_count` 是实际账本记录数。fallback
产生的多条账本会累计 Token，但不会重复增加请求数。结果按 `total_tokens` 降序返回。

跨币种时不要直接相加 `cost_totals_by_currency` 的数值。

## Session Lease 事实

`GET /usage/session-leases` 需要 `usage:read`，默认查询最近 24 小时，跨度不超过 30 天，
可用 `model` 限定 logical model。它返回聚合事实，不包含 Session ID：

| 字段组 | 内容 |
| --- | --- |
| 覆盖 | `routing_decision_count`、`stable_session_decision_count`、覆盖率 |
| 租约 | 置顶与生效次数、`lease_event_counts`、续用与迁移率 |
| 用量 | prompt / 未缓存输入 / 缓存读取 / 缓存写入 Token 与占比 |
| 费用 | `cost_totals_by_currency` 与分 cohort 的 `costed_cohorts` |
| 失败 | attempt、fallback、成功数、限流与 5xx 次数 |
| 门槛 | `facts_complete_for_evaluation` 与 `blocking_reasons` |

`facts_complete_for_evaluation=true` 只表示 Session、租约、provider usage、usage v2、
费用和币种等事实足以比较，不代表值得启用成本路由。Rotor 不给出综合评分，也不修改
路由设置。跨币种窗口会以 `multiple_currencies` 阻止直接比较，应按 logical model、
币种和更窄窗口重新查询。

```bash
curl -H "Authorization: Bearer $ROTOR_CONTROL_API_TOKEN" \
  "http://127.0.0.1:8000/api/control/v1/usage/session-leases?model=gpt-5.4"
```

## 错误码

| 状态 | `code` | 含义 |
| --- | --- | --- |
| 401 | `control_authentication_required` | 缺少 Bearer 凭证 |
| 401 | `control_invalid_credential` | 凭证无效，或使用了 `sk-` 用户 Key |
| 403 | `control_scope_required` | 缺少所需 scope |
| 404 | `channel_not_found` | 渠道不存在 |
| 404 | `request_trace_not_found` | trace 不存在或属于当前 Run |
| 400 | `invalid_channel_query` 等 | 查询参数不合法，例如窗口超限 |
| 503 | `control_api_not_configured` | 未配置固定 Token 且未使用 `rck_` Key |

所有错误都是 `retryable: false`：应先修正凭证、scope 或参数，而不是重试同一请求。

下一步：[MCP 诊断适配器](../guides/mcp.md)。
