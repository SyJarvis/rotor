# 管理 API

管理 API 位于 `/api/admin`，使用服务端管理员 Session 鉴权。首次启动会创建
`admin / 123456`；该账号首次登录后必须修改密码，完成前除认证和改密端点外的管理
接口都返回 `403`。

## 认证端点

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/api/admin/auth/login` | 公开 | 验证用户名和密码，签发 Session 与 CSRF Cookie |
| `GET` | `/api/admin/auth/me` | Session | 返回当前管理员和首次改密状态 |
| `POST` | `/api/admin/auth/change-password` | Session + CSRF | 修改密码并解锁管理接口 |
| `POST` | `/api/admin/auth/logout` | Cookie | 撤销服务端 Session 并清除 Cookie |
| `GET` | `/api/admin/auth/audit-events` | 完整改密后的 Session | 分页读取认证审计事件 |

浏览器管理页会自动处理 Cookie 和 CSRF。命令行调用可以先建立 Cookie jar：

```bash
export ROTOR_ADMIN_COOKIE_JAR=/tmp/rotor-admin.cookies

curl -sS -c "$ROTOR_ADMIN_COOKIE_JAR" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"123456"}' \
  http://127.0.0.1:8000/api/admin/auth/login

export ROTOR_ADMIN_CSRF="$(
  awk '$6 == "rotor_admin_csrf" {print $7}' "$ROTOR_ADMIN_COOKIE_JAR"
)"
```

首次登录还需修改默认密码：

```bash
curl -sS -b "$ROTOR_ADMIN_COOKIE_JAR" \
  -H "X-CSRF-Token: $ROTOR_ADMIN_CSRF" \
  -H "Content-Type: application/json" \
  -d '{
    "current_password":"123456",
    "new_password":"replace-with-a-strong-password"
  }' \
  http://127.0.0.1:8000/api/admin/auth/change-password
```

后续 `GET` 请求携带 `-b "$ROTOR_ADMIN_COOKIE_JAR"`；除退出登录外，`POST`、
`PUT`、`PATCH` 和 `DELETE` 还必须携带 `X-CSRF-Token`。Session 默认空闲 30 分钟、
最长 12 小时。

登录默认按“用户名 + 客户端 IP”限流：5 分钟内失败 5 次后锁定 15 分钟。触发或处于
锁定状态时返回 `429`，`Retry-After` 给出剩余秒数。相关阈值可通过
`ROTOR_ADMIN_LOGIN_MAX_FAILURES`、`ROTOR_ADMIN_LOGIN_WINDOW_SECONDS` 和
`ROTOR_ADMIN_LOGIN_LOCK_SECONDS` 调整。

认证审计记录 `login_succeeded`、`login_failed`、`login_rate_limited`、
`password_changed`、`password_change_failed` 和 `logout`。查询按时间倒序返回，支持
`skip` 与 `limit`；响应包含用户名、结果、原因、客户端 IP、User-Agent 和时间，不含
密码、Session ID、Cookie 或 CSRF：

```bash
curl -b "$ROTOR_ADMIN_COOKIE_JAR" \
  "http://127.0.0.1:8000/api/admin/auth/audit-events?limit=100"
```

## Channels

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/admin/channels` | 列出渠道和统计 |
| `GET` | `/api/admin/channels/presets` | 获取 provider 预设 |
| `GET` | `/api/admin/channels/{id}` | 获取渠道 |
| `POST` | `/api/admin/channels` | 创建渠道 |
| `PUT` | `/api/admin/channels/{id}` | 更新渠道 |
| `DELETE` | `/api/admin/channels/{id}` | 删除渠道 |
| `POST` | `/api/admin/channels/probe-models` | 保存前探测模型 |
| `POST` | `/api/admin/channels/{id}/test` | 测试连通性或能力 |
| `POST` | `/api/admin/channels/{id}/enable` | 启用渠道 |
| `POST` | `/api/admin/channels/{id}/disable` | 禁用渠道 |

Channel 请求字段见 [Channel 字段](channel-schema.md)。
安全 Control API 的 Channel 响应会直接给出生效的 `cache_scope`、`capacity_scope` 和
`billing_scope`，但仍不返回可能包含认证 Header 的完整 `extra`。

## Tokens

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/admin/tokens` | 列出 Token |
| `GET` | `/api/admin/tokens/{id}` | 获取 Token |
| `POST` | `/api/admin/tokens` | 使用请求体创建 Token |
| `POST` | `/api/admin/tokens/generate` | 生成 Token |
| `PUT` | `/api/admin/tokens/{id}` | 更新 Token |
| `DELETE` | `/api/admin/tokens/{id}` | 删除 Token |
| `POST` | `/api/admin/tokens/{id}/enable` | 启用 |
| `POST` | `/api/admin/tokens/{id}/disable` | 禁用 |
| `POST` | `/api/admin/tokens/{id}/reset-quota` | 清零已用配额 |

## MCP Control Keys

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/admin/mcp-control-keys` | 列出 Key 的元数据和掩码提示 |
| `POST` | `/api/admin/mcp-control-keys` | 创建 Key；完整 `rck_` Key 仅在此响应返回一次 |
| `POST` | `/api/admin/mcp-control-keys/{id}/enable` | 启用 Key |
| `POST` | `/api/admin/mcp-control-keys/{id}/disable` | 停用 Key |
| `DELETE` | `/api/admin/mcp-control-keys/{id}` | 删除 Key |

创建请求体为：

```json
{"name": "local-codex"}
```

创建响应还包含 Control API URL。所有这些接口都属于管理面，应只在受信任网络中使用。

## Logs

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/admin/logs` | 分页和过滤请求日志 |
| `GET` | `/api/admin/logs/count` | 返回与列表筛选条件匹配的数量 |
| `GET` | `/api/admin/logs/stats` | 汇总统计 |
| `GET` | `/api/admin/logs/timeseries` | 总体时间序列 |
| `GET` | `/api/admin/logs/timeseries_by_model` | 按模型时间序列 |
| `GET` | `/api/admin/logs/models` | 模型用量 |
| `GET` | `/api/admin/logs/{id}` | 日志详情 |

`period` 可取 `day`、`week` 或 `month`，配合 `period_date=YYYY-MM-DD` 查询指定自然
周期。时间边界和展示规则见[日志、用量与数据存储](../operations/observability.md)。

日志条目会返回 usage schema v2 的 cache read/write 明细、三个资源 scope、
`capacity_snapshot`、`cost_status` 和 tariff 快照。`stats` 与 `models` 使用
`cost_totals_by_currency` 返回费用；只有单币种结果的 `total_cost` 才是数值，混合
币种时为 `null`，调用方不能自行把不同币种直接相加。

## Settings

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/admin/settings` | 当前路由设置、显示时区及文件路径 |
| `PUT` | `/api/admin/settings` | 校验、保存并立即应用路由设置和显示时区 |

更新设置需要提交完整 `ApplicationSettings` 对象，未知字段会被拒绝。
`routing.session_lease_enabled` 控制持久化租约，默认开启；
`routing.session_lease_idle_ttl_seconds` 默认为 900，允许 60–86400 秒。关闭
`routing.affinity_enabled` 时租约不会读取或更新。

## 只读 Session Lease 评估

`GET /api/control/v1/usage/session-leases` 属于 Control API，而不是无独立鉴权的管理
API。它需要 `usage:read`，支持 `start_time`、`end_time` 和 `model`，默认最近 24 小时、
最大 30 天。响应为聚合事实，不包含 Session ID，也不会修改路由。字段口径与数据门槛
见[日志、用量与数据存储](../operations/observability.md#session-lease-评估窗口)。
