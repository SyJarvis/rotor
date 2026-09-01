# 安全边界

Rotor 的客户端 API、管理 API 和 Control API 使用彼此独立的认证边界。

## 客户端 API

生成和状态类请求通过 Rotor Token 认证，接受：

```text
Authorization: Bearer sk-...
x-api-key: sk-...
```

认证会检查 Key 格式、数据库记录、启用状态、过期状态和配额。Token 还可以使用
`allowed_channels` 限制可访问渠道。

`GET /v1/models` 允许不带 Token 调用；提供有效 Token 时会按其渠道权限过滤模型。

## 管理 API

`/api/admin/*` 使用管理员账号和服务端 Session。首次启动创建 `admin / 123456`，
密码以 scrypt 哈希保存；首次登录只得到受限 Session，必须修改密码后才能访问其他
管理接口。浏览器 Session Cookie 使用 `HttpOnly` 和 `SameSite=Strict`，管理写操作
还要求匹配的 CSRF Cookie/Header。

管理员登录限流状态保存在数据库中，默认按规范化用户名和客户端 IP 统计：5 分钟内
失败 5 次后锁定 15 分钟，锁定响应为 `429` 并携带 `Retry-After`。成功登录会清除
该用户名/IP 的失败窗口。客户端 IP 取自 ASGI 连接对端；位于反向代理后时，只应让
应用服务器信任已明确配置的代理来源。

认证审计以追加事件记录登录成功、登录失败、触发锁定、改密成功/失败和退出，包含
管理员、客户端 IP、User-Agent 和时间，但不保存密码、Session Cookie 或 CSRF。
完成首次改密的管理员可通过 `GET /api/admin/auth/audit-events` 读取最近事件。

部署时：

- 在绑定非本机网络地址前完成首次登录和默认密码修改；
- HTTPS 部署设置 `ROTOR_ADMIN_COOKIE_SECURE=true`；
- 使用防火墙、私有网络或受保护的反向代理进一步限制管理面；
- 不可信客户端只能访问所需的 `/v1/*` 或 `/anthropic/v1/*` 路径；
- 管理 API 响应和渠道配置可能涉及上游凭据，应按敏感数据处理。

默认用户名和密码环境变量只在空数据库首次初始化时使用，修改它们不会重置已有
管理员密码。遗失密码时当前版本不提供远程重置端点，应通过受控数据库维护流程恢复。

## 上游 Key

上游供应商 Key 存储在 Channel 数据库记录中，并用于构造 Bearer、`x-api-key`
或 `api-key` 请求头。数据库备份、管理 API 和进程环境都应处于受信任边界。

## 会话和错误正文

默认会保存会话正文和清理后的供应商响应。清理逻辑会递归屏蔽常见敏感字段，但
不能替代数据最小化。无需正文审计时，可以设置：

```env
SAVE_CONVERSATION_BODY=false
SAVE_PROVIDER_RESPONSE=false
```

修改环境变量后重启服务。会话目录和数据库文件应设置合适的文件权限及保留周期。

## 代理请求头

客户端 IP 可能来自 `X-Forwarded-For` 或 `X-Real-IP`。只有在受信任反向代理覆盖
这些头时，日志中的 IP 才可作为可信审计信息。
