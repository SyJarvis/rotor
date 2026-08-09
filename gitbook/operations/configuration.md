# 运行时配置

Rotor 有两类配置：进程启动时读取的环境变量，以及运行中可通过管理 API 修改的
路由设置。

## 环境变量

Pydantic Settings 从当前目录的 `.env` 和进程环境读取大写字段。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///~/.cache/rotor/rotor.db` | SQLAlchemy 异步数据库 URL |
| `API_KEY_PREFIX` | `sk-` | Rotor 用户 Key 格式前缀 |
| `LOG_LEVEL` | `INFO` | 应用日志等级；`DEBUG` 时开放 `/docs` 和 `/redoc` |
| `CORS_ORIGINS` | localhost 的 3000、8000 端口 | 允许的浏览器来源列表 |
| `REQUEST_TIMEOUT` | `120.0` | 上游请求总超时秒数 |
| `CONNECT_TIMEOUT` | `10.0` | 上游连接超时秒数 |
| `WRITE_TIMEOUT` | `30.0` | 上游写入超时秒数 |
| `POOL_TIMEOUT` | `10.0` | HTTP 连接池等待秒数 |
| `CONVERSATION_STORE_ENABLED` | `true` | 是否启用会话存储 |
| `CONVERSATION_STORE_DIR` | `~/.cache/rotor/conversations` | 会话文件目录 |
| `SAVE_CONVERSATION_BODY` | `true` | 是否保存会话请求正文 |
| `SAVE_PROVIDER_RESPONSE` | `true` | 是否保存清理后的上游响应 |
| `CONVERSATION_QUEUE_MAXSIZE` | `10000` | 会话异步写入队列容量 |

列表类型环境变量应使用 Pydantic 可解析的 JSON，例如：

```env
CORS_ORIGINS=["https://admin.example.com"]
```

环境变量在进程启动时读取，修改后需要重启 Rotor。

## 路由设置

路由策略保存在：

```text
~/.rotor/settings.json
```

通过 `GET /api/admin/settings` 查看，通过 `PUT /api/admin/settings` 更新。更改会
立即应用到当前进程，并写入设置文件。字段说明见[选择路由策略](../guides/routing.md)。

## SQLite URL

CLI 会为 SQLite URL 指向的文件创建父目录。使用 PostgreSQL 时，需在启动前确保
数据库已存在且网络可达：

```env
DATABASE_URL=postgresql+asyncpg://user:password@db.example/rotor
```

下一步：[选择部署方式](deployment.md)。
