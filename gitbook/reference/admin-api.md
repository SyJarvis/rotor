# 管理 API

管理 API 位于 `/api/admin`，供浏览器管理页和受信任的自动化使用。当前接口没有
独立管理员认证，部署要求见[安全边界](../operations/security.md)。

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

## Logs

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/admin/logs` | 分页和过滤请求日志 |
| `GET` | `/api/admin/logs/stats` | 汇总统计 |
| `GET` | `/api/admin/logs/timeseries` | 总体时间序列 |
| `GET` | `/api/admin/logs/timeseries_by_model` | 按模型时间序列 |
| `GET` | `/api/admin/logs/models` | 模型用量 |
| `GET` | `/api/admin/logs/{id}` | 日志详情 |

## Settings

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/admin/settings` | 当前路由设置及文件路径 |
| `PUT` | `/api/admin/settings` | 校验、保存并立即应用路由设置 |

更新设置需要提交完整 `ApplicationSettings` 对象，未知字段会被拒绝。
