# 客户端网页登录授权

Rotor 提供 OAuth 授权码与 PKCE 登录。客户端打开 Rotor 网页，用户登录自己的
Rotor 账户并确认授权，客户端随后获得用于模型请求的凭证，用户无需复制 API Key。
这是一套供客户端实现的通用接口；现有 Codex、Claude Code、OpenCode 配置模板
仍使用 API Key，不会自动启用此流程。客户端需要实现下面的登录、续期与退出操作。

登录授权绑定 Rotor 账户，模型权限、可访问渠道、配额及请求记录均归属于该账户。
账户没有 API Key 时也能授权和调用模型；创建、切换、停用或删除 Key 不会影响 OAuth 授权。
API Key 与 OAuth 两种接入方式共同累计账户已用额度。Key 自身的权限和额度限制
只约束通过该 Key 发起的请求，OAuth 使用账户当前的权限和额度。
这类凭证仅用于推理与模型列表，不能访问管理 API 或 Control API。

## 客户端与回调登记

默认公开客户端为 `rotor-client`，没有客户端密钥，支持以下回调路径：

- `http://127.0.0.1:<临时端口>/auth/callback`
- `http://[::1]:<临时端口>/auth/callback`

客户端应先监听本机回环地址，再打开系统浏览器发起授权。只允许回环 IP，
`localhost`、其他 HTTP 主机以及自定义 URI scheme 不在默认登记范围内。
授权请求使用的完整回调地址必须原样用于授权码兑换，包含实际端口。
HTTPS 回调必须与管理员登记的地址完全一致；回调地址不允许查询参数或 fragment。
本机回调与外部浏览器的做法依据 [RFC 8252](https://www.rfc-editor.org/rfc/rfc8252#section-7.3)。

管理员可通过 `ROTOR_OAUTH_CLIENTS` 登记客户端。例如：

```dotenv
ROTOR_OAUTH_ISSUER=https://rotor.example.com
ROTOR_ADMIN_COOKIE_SECURE=true
ROTOR_OAUTH_CLIENTS={"rotor-client":{"name":"Rotor Client","redirect_uris":["http://127.0.0.1/auth/callback","http://[::1]/auth/callback"]},"my-agent":{"name":"My Agent","redirect_uris":["https://agent.example.com/auth/callback"]}}
```

`ROTOR_OAUTH_CLIENTS` 替换完整登记表；保留默认客户端时也需要在表内写出。
部署在反向代理后时，将 `ROTOR_OAUTH_ISSUER` 设为客户端实际访问的 HTTPS 地址，
使发现接口提供正确的外部 URL，并启用安全会话 Cookie。生产环境的授权页面、
兑换与续期请求均应通过 HTTPS；本机回环回调可以使用 HTTP。
当前支持预先登记的公开客户端，不提供动态客户端注册、设备码或密码授权。

## 发起网页登录

客户端为每次登录生成独立的随机 `state` 和 `code_verifier`。
`code_verifier` 为 43–128 个 RFC 7636 允许的字符，推荐使用足够随机的
URL-safe 字符串。计算：

```text
code_challenge = BASE64URL(SHA256(ASCII(code_verifier)))
```

Base64URL 结果去掉 `=` 填充，并把 verifier 和 state 保存在当前登录流程内。
Rotor 仅接受 `S256`，不接受 `plain`。使用 PKCE 与刷新凭证轮换的安全背景见
[RFC 9700](https://www.rfc-editor.org/rfc/rfc9700)。

在浏览器打开以下请求，参数需要 URL 编码：

```text
GET /oauth/authorize
    ?response_type=code
    &client_id=rotor-client
    &redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback
    &scope=inference
    &state=<本次随机值>
    &code_challenge=<计算结果>
    &code_challenge_method=S256
```

用户在 Rotor 网页登录，检查客户端名称、回调地址及授权账户，再选择授权或拒绝。
授权成功返回：

```text
http://127.0.0.1:1455/auth/callback?code=<一次性授权码>&state=<本次随机值>
```

用户拒绝时返回 `error=access_denied` 与 `state`。
客户端收到回调后必须先核对 `state` 与当前登录流程一致，再解析 code 或 error；
不一致时终止登录。授权码 5 分钟内有效且只能兑换一次。回调 URL 只承载授权码，
access token 和 refresh token 通过后续服务端响应获取，不放进 URL。

网页确认使用 `POST /api/oauth/authorization`，JSON 参数与授权请求一致并加上
`approve: true` 或 `false`，通过 Rotor 登录 Cookie 和 `X-CSRF-Token` 保护。
客户端不需要调用该网页接口或收集用户密码。

## 兑换授权码

客户端使用表单请求，而不是 JSON：

```bash
export ROTOR_BASE_URL=https://rotor.example.com

curl "$ROTOR_BASE_URL/oauth/token" \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode 'client_id=rotor-client' \
  --data-urlencode "code=$CODE" \
  --data-urlencode 'redirect_uri=http://127.0.0.1:1455/auth/callback' \
  --data-urlencode "code_verifier=$CODE_VERIFIER"
```

响应格式：

```json
{
  "access_token": "<推理凭证>",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "<续期凭证>",
  "scope": "inference"
}
```

客户端保存凭证并记录到期时间。`access_token` 最长有效 1 小时，不超过授权剩余时间；refresh token
所对应的授权最长为首次授权后的 30 天，续期不延长这个期限。兑换失败时检查
`error` 字段；例如已使用、过期或无法通过 PKCE 验证的授权码返回 `invalid_grant`。
响应带 `Cache-Control: no-store`，日志和错误提示不应记录凭证正文。

## 调用模型接口

推理凭证以 Bearer 方式使用：

```bash
curl "$ROTOR_BASE_URL/v1/models" \
  -H "Authorization: Bearer $ROTOR_ACCESS_TOKEN"

curl "$ROTOR_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $ROTOR_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hello"}]}'
```

同一凭证可用于 `/v1/responses`、`/anthropic/v1/messages` 和对应的模型列表。
Anthropic 请求也使用 `Authorization: Bearer`；客户端 OAuth 不使用 `x-api-key`
传递凭证。授权码和 refresh token 不能用作推理凭证。
账户停用或删除，修改或重置账户密码，或用户撤销授权后，请求立即拒绝；权限和配额
变更也会在请求时检查。授权不会扩大用户已有的模型或渠道权限。

## 续期与退出

使用当前 refresh token 续期：

```bash
curl "$ROTOR_BASE_URL/oauth/token" \
  --data-urlencode 'grant_type=refresh_token' \
  --data-urlencode 'client_id=rotor-client' \
  --data-urlencode "refresh_token=$ROTOR_REFRESH_TOKEN"
```

成功响应包含新的 access token 和 refresh token，客户端应将两者一起替换，并将
同一授权的续期操作串行化。每次续期都会使旧 refresh token 失效；再次提交已用过
的 refresh token 会撤销整项授权，需要重新网页登录。`invalid_grant` 表示该
续期凭证不可用，客户端应清除失效凭证并提示重新登录。配额暂时不足会返回
HTTP 429 与 `error=temporarily_unavailable`，不会消耗本次 refresh token；配额恢复后
可使用同一个 refresh token 重试，无需重新授权。
API Key 停用或耗尽其单独额度不会使 OAuth 授权失效。授权仍有效时可继续使用现有凭证；授权已撤销、到期或密码已变更时
需要重新网页登录。

退出时撤销凭证，再删除本地保存的凭证：

```bash
curl "$ROTOR_BASE_URL/oauth/revoke" \
  --data-urlencode 'client_id=rotor-client' \
  --data-urlencode "token=$ROTOR_REFRESH_TOKEN"
```

撤销接口接受 access token 或 refresh token，撤销其整项授权。用户也可在 Rotor
的 `/oauth/apps` 页面查看并撤销已授权应用；撤销某一项不会停用其他授权或 API Key。

## 服务发现与既有接入

客户端可从 `GET /.well-known/oauth-authorization-server` 获取 issuer、授权、兑换、
撤销接口地址，以及支持的授权类型、scope 和 PKCE 方法。当前 scope 为 `inference`。
发现服务不包含用户凭证，应用需要自行实现以上登录流程。

API Key 接入继续可用，仓库中的客户端配置示例见
[接入 Codex、Claude Code 与 OpenCode](client-integrations.md)。
上游渠道的 [ChatGPT OAuth](chatgpt-oauth.md) 用于 Rotor 访问上游模型；本页描述的是
调用方访问 Rotor 的账户授权，两者分别管理凭证。

## 升级已有授权

已有授权升级后保留授权码、凭证、到期时间、密码版本和撤销状态，绑定关系改为账户。
原 Key 较窄的模型或渠道限制不再约束 OAuth；升级后的请求按账户当前权限检查。
原 Key 的停用、删除或默认 Key 的切换也不会影响这些授权。

升级前请自行保存完整 SQLite 数据库备份。带版本号的数据库正常升级不会自动备份；
只有识别并接纳无版本数据库时，启动迁移才会自动创建备份。
当数据库包含 OAuth 授权码或授权记录时，不能直接降级回依赖 Key 的版本，因为
旧 Key 绑定已移除，无法安全恢复。需要回退时，应恢复升级前保存的数据库备份。
OAuth 授权表为空时仍支持降级。
