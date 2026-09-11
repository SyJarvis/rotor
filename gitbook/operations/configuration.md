# 运行时配置

Rotor 有两类配置：进程启动时读取的环境变量，以及运行中可通过管理 API 修改的
路由设置。

## 环境变量

Pydantic Settings 从当前目录的 `.env` 和进程环境读取大写字段。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///~/.cache/rotor/rotor.db` | SQLAlchemy 异步数据库 URL |
| `API_KEY_PREFIX` | `sk-` | Rotor 用户 Key 格式前缀 |
| `ROTOR_DEFAULT_ADMIN_USERNAME` | `admin` | 空数据库首次初始化时创建的管理员用户名 |
| `ROTOR_DEFAULT_ADMIN_PASSWORD` | `123456` | 空数据库首次初始化密码；首次登录必须修改 |
| `ROTOR_ADMIN_SESSION_IDLE_SECONDS` | `1800` | 管理员 Session 空闲超时 |
| `ROTOR_ADMIN_SESSION_TTL_SECONDS` | `43200` | 管理员 Session 绝对有效期 |
| `ROTOR_ADMIN_COOKIE_SECURE` | `false` | 是否仅通过 HTTPS 发送管理员 Cookie；生产 HTTPS 必须开启 |
| `ROTOR_ADMIN_LOGIN_MAX_FAILURES` | `5` | 同一用户名和客户端 IP 在计数窗口内允许的失败次数 |
| `ROTOR_ADMIN_LOGIN_WINDOW_SECONDS` | `300` | 管理员登录失败计数窗口秒数 |
| `ROTOR_ADMIN_LOGIN_LOCK_SECONDS` | `900` | 达到失败上限后的锁定秒数 |
| `ROTOR_CONTROL_API_TOKEN` | 未设置 | Control API 与 MCP 的固定 Bearer Token；与 `sk-` 用户 Key 不同 |
| `ROTOR_CONTROL_API_SCOPES` | `channel:read`、`request_trace:read`、`usage:read` | Control API 允许的只读 scope |
| `ROTOR_CONTROL_ACTOR_ID` | `rotor-agent` | 写入 Control API 审计事件的操作者标识 |
| `ROTOR_CONTROL_CLIENT_ID` | `rotor-mcp` | 写入 Control API 审计事件的客户端标识 |
| `ROTOR_CONTROL_API_URL` | `http://127.0.0.1:8000/api/control/v1` | MCP 与 MindAgent 使用的 Control API 基址 |
| `ROTOR_MINDAGENT_MCP_COMMAND` | 未设置 | MindAgent 启动独立 MCP Server 的命令 |
| `ROTOR_MINDAGENT_MCP_ARGS` | `[]` | 传给上述命令的参数列表 |
| `ROTOR_MINDAGENT_MCP_CWD` | 未设置 | MCP Server 子进程的工作目录 |
| `ROTOR_LOG_DIR` | `~/.cache/rotor/logs` | 按月分目录、按日保存运行日志的目录 |
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

`ROTOR_DEFAULT_ADMIN_USERNAME` 和 `ROTOR_DEFAULT_ADMIN_PASSWORD` 只在
`admin_users` 为空时生效，不会覆盖已经修改过的密码。

## 路由设置

路由设置和显示时区保存在：

```text
~/.rotor/settings.json
```

通过 `GET /api/admin/settings` 查看，通过 `PUT /api/admin/settings` 更新。更改会
立即应用到当前进程，并写入设置文件。字段说明见[选择路由策略](../guides/routing.md)。

`display_timezone` 必须是有效的 IANA 时区名，默认值为 `Asia/Shanghai`。它只影响
管理页与日志统计中自然日、周和月的边界及时间展示；不会修改已记录请求的时间戳。

Session Lease 与协议亲和运行时字段为：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `routing.affinity_enabled` | `true` | 启用稳定 Session 识别与亲和；关闭时同时停用租约 |
| `routing.session_lease_enabled` | `true` | 启用数据库持久化 Session Lease |
| `routing.session_lease_idle_ttl_seconds` | `1800` | 最后一次成功后的空闲 TTL，范围 60–86400 秒 |
| `routing.session_lease_reassess_seconds` | `300` | 有故障来源证据的异族租约复评间隔；0 关闭 |
| `routing.protocol_affinity_enabled` | `true` | 同协议族渠道优先的软分层；关闭后保留策略排序与租约置顶 |

Channel 级和 logical model 级 TTL 覆盖见 [Channel 字段](../reference/channel-schema.md)。

## SQLite URL

CLI 会为 SQLite URL 指向的文件创建父目录。当前版本的启动迁移路径只接受文件型
SQLite；使用 `postgresql+asyncpg` 等其他驱动时会在启动阶段明确报错退出，不会在缺少
迁移的情况下继续运行。

```env
DATABASE_URL=sqlite+aiosqlite:///~/.cache/rotor/rotor.db
```

下一步：[选择部署方式](deployment.md)。
