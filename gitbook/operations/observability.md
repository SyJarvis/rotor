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

## 请求日志

常用接口：

```text
GET /api/admin/logs
GET /api/admin/logs/{log_id}
GET /api/admin/logs/stats
GET /api/admin/logs/timeseries
GET /api/admin/logs/timeseries_by_model
GET /api/admin/logs/models
```

日志列表可以按时间、模型和成功状态等条件查询。详情可能包含经过清理的上游错误
响应；仍应只向管理员开放。

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
~/.rotor/settings.json
```

SQLite 启用 WAL、5 秒 busy timeout 和 `synchronous=NORMAL`，以减少请求记账和
会话 worker 并发写入时的锁冲突。

## 数据保留

当前实现没有自动清理 request logs、usage ledger、routing decisions、response
routes 或会话文件的保留策略。运维方应根据容量和隐私要求安排备份、归档与删除，
并确保删除操作不会破坏仍在使用的原生 Responses 对象。
