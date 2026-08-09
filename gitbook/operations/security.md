# 安全边界

Rotor 的客户端 API 和管理 API 具有不同的认证现状。部署者必须通过网络边界补齐
管理面保护。

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

当前 `/api/admin/*` 路由没有独立的管理员认证依赖，浏览器管理页也直接调用这些
接口。因此：

- 不要把管理面直接暴露到公网；
- 使用防火墙、私有网络、反向代理认证或等效控制限制访问；
- 不可信客户端只能访问所需的 `/v1/*` 或 `/anthropic/v1/*` 路径；
- 管理 API 响应和渠道配置可能涉及上游凭据，应按敏感数据处理。

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
