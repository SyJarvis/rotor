# 常见错误

本页按症状列出客户端接入时最常见的原因和验证方法。

## `401 Unauthorized`

可能原因：

- 未发送 `Authorization` 或 `x-api-key`；
- 使用了上游供应商 Key，而不是 Rotor 生成的 Key；
- Key 不以配置的 `API_KEY_PREFIX` 开头；
- Token 已禁用、过期或达到配额。

检查：

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $ROTOR_API_KEY"
```

查看 Token 管理记录请登录管理页面；命令行访问管理 API 需要先建立管理员 Session。

## 管理页面要求修改密码

首次启动的默认账号是 `admin / 123456`。该密码只能创建受限 Session，必须在页面中
设置不少于 6 个字符的新密码后才能访问管理功能。若已经修改过密码，启动环境中的
`ROTOR_DEFAULT_ADMIN_PASSWORD` 不会覆盖数据库记录。

## `Model not found`

Rotor 只路由到启用渠道中 `models` 或 `model_mapping` 键包含的模型。还要确认当前
Token 的 `allowed_channels` 没有排除相应渠道：

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $ROTOR_API_KEY"
```

客户端模型名必须使用列表中的值，而不是映射后的供应商模型名。

## `503 Channels temporarily unavailable`

模型和协议都匹配，但当前没有可准入的候选渠道。常见情况：

- 全部兼容渠道都在冷却期（例如刚发生过 429 或 5xx）；
- 冷却已到期，但同一 `(model, channel_id)` 的恢复探针已被另一个并发请求占用。

响应会带 `Retry-After` 给出建议重试秒数。恢复探针由后续业务请求触发，没有定时后台
探活，因此持续请求会在探针返回后自然恢复；没有必要手工重启服务。

如果长时间不恢复，检查上游是否仍在失败，以及 `extra.fallback_cooldown_seconds`
是否被设得过大。

## `No compatible channel available`

模型存在，但没有渠道满足请求能力。常见情况：

- Responses 请求使用了必须原生支持的 hosted tool 或状态化能力；
- 渠道 `extra.capabilities` 缺少请求要求的能力；
- 只有原生协议之外的渠道可用；
- Token 渠道限制排除了兼容渠道。

通过管理页检查渠道协议和能力探测结果。原生 Responses 功能需要
`protocol=openai_responses`。

## Claude Code 请求 `/v1/messages` 返回 404

Claude Code 的 Base URL 少了 `/anthropic`。正确关系是：

```text
ANTHROPIC_BASE_URL=http://127.0.0.1:8000/anthropic
最终请求路径=http://127.0.0.1:8000/anthropic/v1/messages
```

## OpenCode 请求了错误路径

Responses 配置应使用仓库的 `opencode-responses.json`，Chat Completions 配置应
使用 `opencode-chat.json`。对应 SDK provider 不可混用。

## 渠道测试成功，但生成请求失败

普通渠道测试使用 `GET /models`，只证明模型发现入口和认证基本可用。生成能力、
模型权限、工具调用和流式协议仍可能失败。对目标模型执行显式能力测试，并注意该
测试可能计费。

流式请求中断或没有自动切换渠道时，继续阅读[流式响应与故障转移](streaming-and-fallback.md)。
