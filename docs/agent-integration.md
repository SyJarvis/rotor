# Coding Agent 接入规范

本文面向需要接入 Rotor 网关的 coding agent(pi agent、opencode、Claude Code、Codex CLI 等)及其配置者,定义认证方式、发现端点、推理端点与错误语义。所有端点以公网部署为例,自建实例请替换域名。

## 总览

Rotor 对外提供两种协议面,base_url 二选一:

| 协议 | base_url | 适用 |
|---|---|---|
| OpenAI 兼容 | `https://llm.taihua.site/v1` | OpenAI SDK、Codex CLI、opencode、pi agent 等 |
| Anthropic 兼容 | `https://llm.taihua.site/anthropic` | Claude Code、Anthropic SDK |

两个协议面共享同一套渠道与账号体系,认证方式通用。

## 认证

### 方式一:Rotor API key(简单)

管理控制台创建的 `sk-` 前缀令牌:

- OpenAI 面:`Authorization: Bearer <key>`
- Anthropic 面同样支持 Bearer,或按 Anthropic 惯例放 `x-api-key: <key>`

### 方式二:OAuth(面向 agent 的浏览器登录)

标准授权码流程 + PKCE,元数据发现:

```
GET https://llm.taihua.site/.well-known/oauth-authorization-server
```

流程(与 Codex CLI 登录 ChatGPT 同款模式):

1. agent 在本机起一个 HTTP 回调 server(默认客户端 `rotor-client` 注册的回调是 `http://127.0.0.1/auth/callback`,可按需在服务端 `ROTOR_OAUTH_CLIENTS` 增加客户端与回调地址)
2. 生成 `code_verifier` 与 `code_challenge`(S256),打开浏览器访问:

```
GET /oauth/authorize?
    response_type=code
    &client_id=rotor-client
    &redirect_uri=http://127.0.0.1/auth/callback
    &scope=inference
    &state=<随机串>
    &code_challenge=<S256(verifier)>
    &code_challenge_method=S256
```

3. 用户在浏览器登录 Rotor 账号并确认授权,code 回落到本地回调
4. 换取令牌:

```
POST /oauth/token
    grant_type=authorization_code
    code=<回调拿到的 code>
    code_verifier=<步骤2的verifier>
    client_id=rotor-client
    redirect_uri=...
```

5. 返回 `rotor-access-`(access token,1 小时)+ refresh token(授权有效期 30 天)

调用推理与发现端点时:`Authorization: Bearer rotor-access-...`。access token 过期前用 refresh 换新:

```
POST /oauth/token
    grant_type=refresh_token
    refresh_token=...
    client_id=rotor-client
```

撤销授权:`POST /oauth/revoke`。

**收到 401 时**:先用 refresh_token 续期重试;refresh 也失败(超过 30 天或被撤销)才重新走浏览器授权。

## 发现端点(探测三件套)

### GET /v1/models —— 模型列表 + 认证探测

列出当前凭据可用的全部模型(含渠道模型映射别名)。agent 启动时调它即可同时完成"连通 + 授权"探测:401 = 凭据失效,200 = 就绪。

```json
{"object": "list", "data": [
  {"id": "gpt-6", "object": "model", "created": 1792831349, "owned_by": "rotor",
   "display_name": "GPT-6", "max_input_tokens": 272000, "max_tokens": 128000}
]}
```

- `owned_by` 恒为 `rotor`,不暴露渠道信息
- `display_name` / `max_input_tokens` / `max_tokens` / `capabilities` 为可选能力元数据,来自渠道配置 `extra.model_info`;未配置时省略,标准 OpenAI 客户端不受影响
- 匿名访问返回 401

### GET /v1/models/{id} —— 单模型描述

与列表同构。404 不区分"模型不存在"与"当前凭据无权限",避免探测出隐藏模型。

### GET /v1/usage —— 配额与上游计划窗口

当前凭据的用量快照,agent 可据此辅助节流与窗口提示。以下数据为合成示例,不是账户实测记录；假设查询时间为 2030-01-08 06:00 UTC:

```json
{
  "quota": {"used": 200, "limit": 1000, "unit": "tokens"},
  "channels": [
    {"channel_id": 9, "channel": "openai-plus", "provider": "chatgpt", "plan": "plus",
     "rate_limit": {
       "primary_window":  {"used_percent": 7.0, "window_seconds": 18000,  "reset_after_seconds": 300, "reset_at": 1894082700.0},
       "secondary_window": {"used_percent": 15.0, "window_seconds": 604800, "reset_after_seconds": 400000, "reset_at": 1894482400.0}
     },
     "limit_reached": false, "allowed": true, "subscription_status": null, "fetched_at": 1894082400.0}
  ]
}
```

- `quota`:Rotor 自身的令牌配额,`null` 表示不限量
- `channels[].rate_limit` 是通用窗口:ChatGPT、智谱、Command Goat、OpenCode 的 `primary_window` 为 5 小时窗、`secondary_window` 为周窗,字段可为 `null`;时间为 Unix 秒,`reset_after_seconds` 相对 `fetched_at`,不是读取缓存时的倒计时。
- 仅输出当前凭据允许的且已启用的渠道；模型权限受限时,还须至少提供一个允许的模型名或映射别名。还须服务商条件有效、缓存与当前配置匹配。窗口数据来自 90 秒内的管理侧缓存,此接口**不会**触发上游请求;`channels` 为空也可能是权限、模型、渠道状态或缓存条件不符,不代表额度耗尽。
- ChatGPT/智谱的 `limit_reached` 可反映窗口耗尽;`null` 表示未知。Goat/OpenCode 的通用 `limit_reached` 始终为 `null`,不能作为全部模型不可用的判定。Goat 专属详情仅保留窗口限制开关和各窗口 `exceeded`、`reset_at`;OpenCode 专属详情仅保留三个 usage 窗口的 `status`、`used_percent`、`reset_at`。窗口受限不保证免费模型或付费额度不可用。
- 此客户端接口不暴露上游余额、成本、原始金额额度或专属订阅账务详情；管理员接口仍可查看完整类型化数据。

### Anthropic 面

- `GET /anthropic/v1/models`:`type: "model"` 形状,支持 `limit` / `after_id` / `before_id` 分页,同样强制认证
- `POST /anthropic/v1/messages/count_tokens`:发请求前估算上下文长度,agent 决定何时压缩历史

## 推理端点矩阵

| 端点 | 协议面 | 说明 |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI | Chat Completions,支持流式 |
| `POST /v1/responses` | OpenAI | Responses API(含 `input_tokens`/`compact`/get/`cancel`) |
| `POST /v1/embeddings` | OpenAI | 向量 |
| `POST /v1/images/generations` | OpenAI | 生图 |
| `POST /anthropic/v1/messages` | Anthropic | Messages,支持流式 |
| `POST /anthropic/v1/messages/count_tokens` | Anthropic | token 估算 |

## 错误语义与重试建议

| 状态 | 含义 | agent 行为 |
|---|---|---|
| 401 `authentication_failed` | 凭据失效 | refresh 换新后重试一次;再失败则重新授权 |
| 404 `model_not_found` | 模型不存在或无权限 | 不要重试;从模型列表重新选取 |
| 429 | 该模型的所有渠道都在冷却 | 按 `Retry-After` 头 / `error.retry_after_seconds` 退避;`error.type=usage_limit_reached` 表示是额度窗口耗尽,`error.resets_at` 是本地计算的重置时刻 |
| 5xx | 上游不可用 | 指数退避重试 |

冷却导致的 429 响应体(agent 可直接读取退避时间):

```json
{"error": {"message": "Channels for model 'glm-5.3' are cooling down until the usage window resets; retry in 1742s",
           "type": "usage_limit_reached", "code": "channels_temporarily_unavailable",
           "retry_after_seconds": 1742, "resets_at": 1792923902}}
```

另外,当请求发生渠道切换(fallback)时,成功响应会带 `X-Rotor-Fallback: true` 和 `X-Rotor-Fallback-Reason: <渠道>: <错误码>` 头,便于排查为何没有由首选渠道服务。

## 各 agent 配置样例

### 通用 OpenAI SDK / pi agent / opencode

```python
from openai import OpenAI
client = OpenAI(base_url="https://llm.taihua.site/v1", api_key="<sk-或 rotor-access-...>")
models = client.models.list()
```

opencode 的 provider 配置同理:`baseURL` 指向 `/v1`,填 Rotor 令牌。

### Claude Code

```bash
export ANTHROPIC_BASE_URL=https://llm.taihua.site/anthropic
export ANTHROPIC_AUTH_TOKEN=<sk-或 rotor-access-...>
claude
```

### Codex CLI(`~/.codex/config.toml`)

```toml
[model_providers.rotor]
name = "Rotor"
base_url = "https://llm.taihua.site/v1"
env_key = "ROTOR_API_KEY"
wire_api = "responses"   # 或 "chat"
```

## 最小演练

```bash
BASE=https://llm.taihua.site
KEY=<你的令牌>

# 1. 探测:列出可用模型(401 则先解决凭据)
curl -s $BASE/v1/models -H "Authorization: Bearer $KEY"

# 2. 配额与上游窗口
curl -s $BASE/v1/usage -H "Authorization: Bearer $KEY"

# 3. 发一次请求
curl -s $BASE/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "<上一步列出的模型>", "messages": [{"role": "user", "content": "ping"}]}'
```
