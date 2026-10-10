# 安全边界

Rotor 的客户端 API、管理 API 和 Control API 使用彼此独立的认证边界。

## 客户端 API

生成和状态类请求接受 Rotor API Key 或客户端 OAuth access token：

```text
Authorization: Bearer sk-...
Authorization: Bearer <Rotor OAuth access token>
x-api-key: sk-...
```

`x-api-key` 仅接受 API Key。授权码和 refresh token 不能用于推理，OAuth access token
不能访问管理 API 或 Control API。账户网页登录 Cookie 用于网页和个人接口，不替代
客户端推理认证头。

认证会检查 Key 格式、数据库记录、启用状态、过期状态和配额。账户所属 Key 同时
检查账户状态、配额和账号与 Key 的渠道、模型权限交集；无所属账户的旧 Key 保留
自身权限和独立配额。网页对话与 OAuth 直接检查账户权限和配额，不依赖基础或默认
Key；三种账户接入方式共同累计账户已用额度。

`GET /v1/models` 允许无凭证调用，列出启用渠道的模型及映射键；提供有效 API Key
或 OAuth access token 时，按实际渠道和模型权限过滤。

## 客户端网页登录授权

Rotor 的客户端登录使用授权码与 PKCE S256，授权绑定账户。访问凭证、刷新凭证和
授权码只在数据库中保存哈希；续期轮换刷新凭证，复用旧刷新凭证会撤销整项授权。
账户密码变更或重置后，已有 OAuth 凭证不能继续使用，需要重新授权。

“设置 → 登录与安全 → 设备管理”显示当前账户的每次客户端授权，包括授权时间、
最后使用时间和到期时间。它是授权记录列表，不识别物理设备，也不提供在线状态、
操作系统或 IP。撤销一项会使该项的访问及刷新凭证失效，不影响其他授权或 API Key。
旧地址 `/oauth/apps` 会跳转到 `/?settings=security`。流程和配置见
[客户端网页登录授权](../guides/client-oauth.md)。

## 管理 API

管理功能使用 `admin` 角色账户和服务端 Session；普通账户仅能访问自己的网页对话、
个人资料、Key、日志和客户端授权。首次启动创建 `admin / 123456`，
密码以 scrypt 哈希保存；首次登录只得到受限 Session，必须修改密码后才能访问其他
管理接口。浏览器 Session Cookie 使用 `HttpOnly` 和 `SameSite=Strict`，管理和个人写操作
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
- 仅使用 API Key 的模型客户端按需开放 `/v1/*` 和 `/anthropic/v1/*`；客户端 OAuth
  还需服务发现、`/oauth/*` 以及浏览器登录、授权所需页面和个人认证接口，范围见
  [客户端网页登录授权](../guides/client-oauth.md)；
- 管理 API 响应和渠道配置可能涉及上游凭据，应按敏感数据处理。

默认用户名和密码环境变量只在空数据库首次初始化时使用，修改它们不会重置已有
管理员密码。已登录且完成首次改密的管理员可通过
`POST /api/admin/users/{user_id}/reset-password` 配合 CSRF 重置指定账户密码；该操作
撤销其全部网页 Session，并要求下次登录改密。没有公开的密码找回接口；若已无可用的
管理员访问，应通过受控数据库维护流程恢复。

自行修改密码会保留当前网页 Session、撤销该账户其他 Session，并使旧 OAuth 授权
失效。退出网页登录只撤销当前 Session，不自动撤销客户端 OAuth 授权。改密与重置
不会删除 API Key；账户必须改密期间，账户所属 Key 的请求也会被拒绝。

## 上游凭证

上游供应商 Key 存储在 Channel 数据库记录中，并用于构造 Bearer、`x-api-key`
或 `api-key` 请求头。[ChatGPT OAuth 渠道](../guides/chatgpt-oauth.md)则由后端持有上游
访问和刷新凭证，只使用 Responses 上行。它与客户端登录 Rotor 的 OAuth 分别管理
凭证；上游凭证不通过渠道详情返回浏览器，也不能放入 `extra`。数据库中包含这些
敏感凭证，备份、管理 API 和进程环境都应处于受信任边界。

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
