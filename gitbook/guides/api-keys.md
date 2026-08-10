# 管理 API Key 与配额

Rotor API Key 用于认证客户端请求，并可限制配额、有效期和允许访问的渠道。
它不同于存放在 Channel 中的上游供应商 Key。

## 生成 Key

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/admin/tokens/generate?name=team-a&quota=1000000"
```

`quota` 的单位是 Token；省略或设为空表示不限制。响应中的 `key` 是供客户端
使用的 Rotor Key，应将其视为凭据保存。

## 客户端认证

OpenAI 风格客户端通常使用 Bearer Token：

```text
Authorization: Bearer sk-...
```

Anthropic 风格请求也可以使用：

```text
x-api-key: sk-...
```

Rotor 同时接受这两种方式。Key 必须以 `API_KEY_PREFIX` 开头，默认前缀为 `sk-`。

## 限制可访问渠道

Token 的 `allowed_channels` 是渠道 ID 列表。值为 `null` 时可以访问所有启用渠道；
设置列表后，模型发现和请求路由都会限制在这些渠道内。

```json
{
  "allowed_channels": [1, 3],
  "quota": 1000000,
  "enabled": true
}
```

## 生命周期操作

管理 API 支持：

- `POST /api/admin/tokens/{id}/disable`
- `POST /api/admin/tokens/{id}/enable`
- `POST /api/admin/tokens/{id}/reset-quota`
- `PUT /api/admin/tokens/{id}`
- `DELETE /api/admin/tokens/{id}`

当 `used_quota` 达到 `quota` 时，后续请求会被拒绝；成功记账也会将该 Token
标记为禁用。流式上游没有返回 usage 时，Rotor 会把用量来源记录为 `missing`，
而不是猜测 Token 数量。

管理接口本身的安全边界见[安全边界](../operations/security.md)。
