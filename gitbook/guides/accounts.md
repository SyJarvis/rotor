# 管理账户与邀请

Rotor 的登录账户统一保存在 `admin_users` 表中。`admin` 角色可以管理渠道、账户、
API Key 和日志，`user` 角色可以使用网页对话、自己的 API Key 和客户端授权。
两类账户都可以登录网页，区别只在于是否具备管理员权限。

## 创建账户

管理员在“用户管理”页点击“创建用户”，或调用管理接口：

```bash
curl -X POST -b "$ROTOR_ADMIN_COOKIE_JAR" \
  -H "X-CSRF-Token: $ROTOR_ADMIN_CSRF" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "team-a",
    "password": "replace-with-a-strong-password",
    "display_name": "Team A",
    "role": "user",
    "quota": 1000000,
    "allowed_channels": [],
    "allowed_models": null
  }' \
  http://127.0.0.1:8000/api/admin/users
```

用户名只允许字母、数字、`_`、`.`、`@` 和 `-`，长度 1–100；密码长度 6–128。新建账户
会被标记为“首次登录必须改密”，在完成改密前该账户的模型接口返回 403。

创建账户时会自动生成一个属于该账户的 `base` Key，并作为默认 Key。这个 Key 不需要
手工保存：账户所有后续 Key 都由它派生配额与权限检查。

## 额度与权限字段

| 字段 | 含义 |
| --- | --- |
| `quota` | 账户可用 Token 总额度；`null` 表示不限制，`0` 表示不能再发起推理 |
| `used_quota` | 已用 Token，只能通过重置或消耗变化 |
| `allowed_channels` | 允许使用的渠道 ID 列表；`null` 不限制，空列表不允许任何渠道 |
| `allowed_models` | 允许使用的模型名列表；`null` 不限制，空列表不允许任何模型 |

网页对话、该账户的 API Key 和客户端 OAuth 请求共同累计 `used_quota`。额度耗尽后，
账户范围请求返回 `429 Account token quota exceeded`；管理页显示“已用 / 总额度”。

账户名下的 API Key 还有自己的 `quota`、`allowed_channels` 和 `allowed_models`，实际
权限是账户与 Key 两层限制的交集。创建或修改 Key 时，Key 的限制不能超过账户已有的
限制，否则返回 `403`。

修改账户权限：

```bash
curl -X PATCH -b "$ROTOR_ADMIN_COOKIE_JAR" \
  -H "X-CSRF-Token: $ROTOR_ADMIN_CSRF" \
  -H "Content-Type: application/json" \
  -d '{"quota": 2000000, "allowed_models": ["glm-4.7"]}' \
  http://127.0.0.1:8000/api/admin/users/2
```

把 `role` 从 `admin` 改为 `user`，或把 `enabled` 设为 `false`，都会立即删除该账户的
全部网页 Session，使其现有登录失效。系统拒绝停用或降级最后一个启用的管理员。

## 邀请码注册

不需要管理员逐个建号时，可以生成邀请码让用户自行注册：

1. 在“用户管理”页点击“生成邀请码”，或调用 `POST /api/admin/invitations`。
2. 把返回的 `token` 交给用户。邀请码只在创建响应中出现一次，Rotor 只保存它的哈希。
3. 用户打开 `http://127.0.0.1:8000/?register=1`，填写邀请码、用户名和密码完成注册。

邀请码字符串与 API Key 使用同一个前缀和随机格式，但它只能用于注册，不能访问模型
接口或管理接口。

邀请码可预置与账户相同的 `quota`、`allowed_channels` 和 `allowed_models`，注册成功后
这些限制写入新账户。有效期 `expires_in_hours` 默认 72 小时，范围 1–720 小时。

```bash
curl -X POST -b "$ROTOR_ADMIN_COOKIE_JAR" \
  -H "X-CSRF-Token: $ROTOR_ADMIN_CSRF" \
  -H "Content-Type: application/json" \
  -d '{"expires_in_hours": 72, "quota": 200000, "allowed_models": null}' \
  http://127.0.0.1:8000/api/admin/invitations
```

邀请码有效、未使用、未被撤销时只能成功注册一次。注册页与登录共用同一套失败限流，
按客户端 IP 计数。撤销邀请码使用
`DELETE /api/admin/invitations/{invitation_id}`；已使用的邀请码会显示为已使用，
不再出现在可撤销状态。

## 重置密码

管理员可以重置任意账户的密码：

```bash
curl -X POST -b "$ROTOR_ADMIN_COOKIE_JAR" \
  -H "X-CSRF-Token: $ROTOR_ADMIN_CSRF" \
  -H "Content-Type: application/json" \
  -d '{"password": "replace-with-a-strong-password"}' \
  http://127.0.0.1:8000/api/admin/users/2/reset-password
```

重置会撤销该账户全部网页 Session、使旧客户端 OAuth 授权失效，并要求用户下次登录后
改密。这不是公开的密码找回接口，Rotor 不发送邮件，也不提供自助重置。

## 账户自助操作

普通用户可以在“设置 → 账户”修改显示名称，在“设置 → 登录与安全”修改密码。个人的
客户端授权记录也在同一页面撤销，详见[客户端网页登录授权](client-oauth.md)。

个人资料接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/me` | 当前账户资料、额度与权限 |
| `PATCH` | `/api/me` | 修改 `display_name` |
| `GET` | `/api/me/tokens` | 列出属于当前账户的 API Key |
| `POST` | `/api/me/tokens` | 创建属于当前账户的 API Key |
| `PATCH` | `/api/me/tokens/{id}` | 修改名称、额度、权限或启停 |
| `POST` | `/api/me/tokens/{id}/rotate` | 轮换 Key 值 |
| `POST` | `/api/me/tokens/{id}/default` | 设为默认 Key |
| `DELETE` | `/api/me/tokens/{id}` | 删除 Key |

删除默认 Key 之前必须先选择另一个默认 Key，否则返回 `409`。相关字段见
[管理 API Key 与配额](api-keys.md)；网页对话与账户额度的关系见[使用网页对话](chat.md)。

## 相关配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ROTOR_DEFAULT_ADMIN_USERNAME` | `admin` | 空库首次初始化时创建的管理员 |
| `ROTOR_DEFAULT_ADMIN_PASSWORD` | `123456` | 首次初始化密码，登录后必须修改 |
| `ROTOR_ADMIN_LOGIN_MAX_FAILURES` | `5` | 同一用户名与 IP 的失败次数上限 |
| `ROTOR_ADMIN_LOGIN_WINDOW_SECONDS` | `300` | 失败计数窗口 |
| `ROTOR_ADMIN_LOGIN_LOCK_SECONDS` | `900` | 达到上限后的锁定时间 |

默认账号只在 `admin_users` 为空时创建，不会覆盖已修改的密码。对外暴露服务前应先在本机
完成首次登录与改密，并阅读[安全边界](../operations/security.md)。
