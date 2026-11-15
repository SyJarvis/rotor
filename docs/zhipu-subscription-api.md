# 智谱订阅信息接口

本文记录 Rotor 当前智谱 GLM Coding Plan 订阅查询的实现约定，依据为提交 `e373c4a` 对应的代码、仓库测试及 2026-10-30 只读查询记录的中国站未订阅响应。它不是上游发布的完整或稳定接口规范；测试示例与真实返回的验证边界在下文分别标明；文中账户相关数值与时间仅为合成测试数据，不是实测账户数据。

## 地址与认证

订阅额度查询使用 `GET`，路径接到官方 origin，而不是模型调用的 base path 后面：

| 渠道官方主机 | 额度接口 |
|---|---|
| `open.bigmodel.cn` | `https://open.bigmodel.cn/api/monitor/usage/quota/limit` |
| `api.z.ai` | `https://api.z.ai/api/monitor/usage/quota/limit` |

例如渠道 base URL 为 `https://open.bigmodel.cn/api/coding/paas/v4` 时，额度路径不会拼成 `/api/coding/paas/v4/api/monitor/...`。中国站与 Z.ai 分别使用自己的 origin。

```http
Authorization: <API Key>
Accept: application/json
```

这里的 `Authorization` 使用**裸 API Key，不加 `Bearer ` 前缀**。这与模型推理接口的认证配置不是同一个约定。Python 请求设置 `timeout=15.0`、`follow_redirects=False`。

以下命令仅用于说明请求格式。通过环境变量注入密钥，不要把真实密钥写进脚本、版本库或调试日志，也不要启用 shell 的 `set -x`：

```bash
: "${API_KEY:?请先设置 API_KEY 环境变量}"

curl --fail-with-body --silent --show-error --max-time 15 \
  --header "Authorization: ${API_KEY}" \
  --header 'Accept: application/json' \
  'https://open.bigmodel.cn/api/monitor/usage/quota/limit'
```

Z.ai 使用表中对应地址；命令不使用 `--location`，不跟随重定向。

## 服务商选择与适用范围

Rotor 仅对同时满足下列条件的渠道执行智谱订阅查询：

- 服务商明确选择 Zhipu，即 `type: "zhipu"`。
- 渠道认证类型为 `auth_type: "api_key"`。
- URL 使用 HTTPS，主机精确等于 `open.bigmodel.cn` 或 `api.z.ai`。
- 端口为默认端口或 `443`，URL 中没有用户名、密码。

`openai`、`anthropic`、`command` 等其他服务商即使填写智谱官方地址，也不会触发智谱查询。相似域名、仅在路径或查询字符串中出现官方域名、HTTP、非标准端口均不符合条件。`protocol` 是独立配置：`type: "zhipu"` 可以使用 `openai` 或 `anthropic` 协议。系统不根据域名自动更改渠道服务商。

管理端在读取额度缓存前检查当前服务商条件，不支持的渠道返回 `400` 并清除该渠道旧缓存；`/v1/usage` 不输出这类旧快照。OAuth 保留其独立的订阅查询流程，不进入智谱 API Key 查询或未订阅停用逻辑。

查询范围与自动停用范围不同：上述官方主机的智谱 API Key 渠道可以查询额度，但只有后文列出的 Coding Plan 路径可以因未订阅被持久化停用。

实现依据：[智谱服务](../src/rotor/services/zhipu_quota.py)、[管理端渠道接口](../src/rotor/api/admin/channels.py)、[客户端使用量接口](../src/rotor/api/v1/usage.py)。

## 个人与团队请求

个人查询不附加组织查询参数。团队配置从渠道的 `extra.headers` 读取，仅转发以下两个请求头：

| 请求头 | 用途 |
|---|---|
| `bigmodel-organization` | 组织范围；代码检测到该小写键的非空值时增加 `?type=2` |
| `bigmodel-project` | 项目范围；仅提供项目头不会触发 `type=2` |

建议统一使用小写键。当前实现以 `name.lower()` 判断是否在白名单中，但保留原键名写入 Python 字典；是否添加 `type=2` 使用区分大小写的 `headers.get("bigmodel-organization")`。因此不能承诺 `Bigmodel-Organization` 等写法也会触发团队参数。

以下为配置示意，组织和项目值均为占位符，不是实测账户信息：

```json
{
  "type": "zhipu",
  "auth_type": "api_key",
  "extra": {
    "headers": {
      "bigmodel-organization": "<组织ID>",
      "bigmodel-project": "<项目ID>"
    }
  }
}
```

其他 `extra.headers` 不会转发到额度接口，也不会覆盖这里使用的裸密钥认证头。截至 2026-10-30，本文没有团队接口的线上验证记录，以下仅说明请求格式：

```bash
: "${API_KEY:?请先设置 API_KEY 环境变量}"
: "${ORGANIZATION_ID:?请先设置 ORGANIZATION_ID 环境变量}"
: "${PROJECT_ID:?请先设置 PROJECT_ID 环境变量}"

curl --fail-with-body --silent --show-error --max-time 15 \
  --header "Authorization: ${API_KEY}" \
  --header 'Accept: application/json' \
  --header "bigmodel-organization: ${ORGANIZATION_ID}" \
  --header "bigmodel-project: ${PROJECT_ID}" \
  'https://open.bigmodel.cn/api/monitor/usage/quota/limit?type=2'
```

## 正常额度响应与字段映射

下面参照仓库 `test_parse_classifies_windows_by_unit_not_order` 的模拟 fixture 构造，**不是正常订阅账户的完整线上响应**。假设查询时间为 2030-01-08 06:00 UTC，时间戳是合成数据，不代表真实账户的重置周期：

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": {
    "limits": [
      {
        "type": "TOKENS_LIMIT",
        "unit": 6,
        "percentage": 8,
        "nextResetTime": 1894579200000
      },
      {
        "type": "TOKENS_LIMIT",
        "unit": 3,
        "percentage": 12,
        "nextResetTime": 1894100400000
      }
    ],
    "level": "pro"
  },
  "success": true
}
```

| 上游字段 | Rotor 映射与当前行为 |
|---|---|
| `data` | 必须是对象 |
| `data.level` | 有值时转换为字符串作为 `plan`；缺失或空值时为 `null` |
| `data.limits` | 必须是数组；没有可识别窗口时报查询错误 |
| 窗口 `type` | 识别 `TOKENS_LIMIT`、`CREDIT_LIMIT`，使用下述优先策略 |
| 窗口 `unit: 3` | `primary`，`window_seconds: 18000`，代表 5 小时窗口 |
| 窗口 `unit: 6` | `secondary`，`window_seconds: 604800`，代表每周窗口 |
| 窗口 `percentage` | 已用百分比，传给 `used_percent`；不是剩余百分比 |
| 窗口 `nextResetTime` | 仅数值且大于 `10**12` 时除以 1000，生成 Unix 秒的 `reset_at` |
| 换算后的重置时间 | `reset_after_seconds = max(0, round(reset_at - fetched_at))` |

`nextResetTime` 缺失、为 `null`、为 `0`，或不满足上述数值与阈值检查时，`reset_at`、`reset_after_seconds` 均为 `null`。不会补造“现在加 5 小时”，也不会将 `0` 展示成 1970 年。

当前智谱实现没有主动将 `percentage` 限制在 0–100，也没有对所有数值做有限数或严格类型检查。本文描述已实现的映射，不承诺任意畸形字段都会变成统一的 `502`；不要将其他服务商的严格校验规则套到这里。

窗口选择的顺序是：

1. 先扫描 `TOKENS_LIMIT`，只接受 `unit` 为 `3` 或 `6` 的项，同类型、同 `unit` 取第一项。
2. 只要找到任意一个可识别的 `TOKENS_LIMIT` 窗口，就使用这一类型并停止；即使另一窗口缺失，也不从 `CREDIT_LIMIT` 补齐。
3. 仅当没有可识别的 `TOKENS_LIMIT` 窗口时，才扫描 `CREDIT_LIMIT`，同样按 `unit` 分类并取第一项。
4. 两类都没有可识别窗口时，查询失败。不能按列表位置或重置时间排序来推断窗口类型。

任一已选窗口的 `used_percent >= 100` 时，`limit_reached=true`、`allowed=false`；否则分别为 `false`、`true`。正常额度响应的 `subscription_status` 当前为 **`null`，不是 `"active"`**。缺少 `level` 不会被判定为未订阅。

`fetch_zhipu_usage()` 产生的 `channel_enabled` 为 `null`，由管理端或后台刷新流程补入实际启停状态。`allowed` 是额度窗口计算结果，不会覆盖渠道的 `enabled` 配置。

依据：[额度字段模型](../src/rotor/schemas/channel.py)、[解析与请求测试](../tests/test_zhipu_quota.py)。`CREDIT_LIMIT` 的回退行为在同文件的 `test_parse_falls_back_to_credit_limit_entries` 中使用模拟数据验证。

## 已确认未订阅与查询失败

2026-10-30 的中国站只读查询记录了以下未订阅返回，HTTP 状态为 **`200`**：

```json
{
  "code": 500,
  "success": false,
  "msg": "当前用户不存在coding plan",
  "data": null
}
```

这里的业务字段 `code: 500` **不是 HTTP 500**。只有 HTTP 请求成功后，以下三个条件同时成立，当前实现才判定为未订阅：

- `success` 是布尔值 `false`，代码使用 `is False`。
- `code == 500`。
- `msg` 精确等于 `当前用户不存在coding plan`。

`data: null` 是该查询记录的返回值，不是额外的匹配条件。命中后返回 `subscription_status: "inactive"`、`allowed: false`；`plan`、`limit_reached`、`primary`、`secondary` 均为 `null`。这表示没有订阅，不表示“额度已经用掉 100%”。

| 情况 | 当前 Rotor 行为 |
|---|---|
| HTTP 成功且命中上述三个业务条件 | 正常返回未订阅快照，不报“额度获取失败” |
| 上游 HTTP `401`、`403` | 管理端返回 `502`，作为认证失败处理，不判定未订阅 |
| 上游其他非 2xx，包括 `429`、`5xx`、重定向 | 管理端返回 `502`，不判定未订阅 |
| 网络异常或超时 | 管理端返回 `503`，不判定未订阅 |
| `success: false` 但业务码或消息不匹配 | 管理端返回 `502`，不判定未订阅 |
| 非 JSON、顶层非对象、`data` 非对象、`limits` 非数组、无可识别窗口 | 这些显式处理的响应错误返回 `502`，不判定未订阅 |

例如 HTTP `500` 即使附带同样的 JSON，也会先被视为上游 HTTP 错误。认证失败、超时及其他查询失败不会触发未订阅停用。这张表只涵盖代码显式处理的分支，不代表完整的上游错误规范。

## Rotor 接口、缓存与渠道状态

管理端复用 `GET /api/admin/channels/{channel_id}/oauth/usage`，路径中的 `oauth` 是既有名称，同样支持智谱 API Key。默认使用 90 秒内的进程内缓存；`?refresh=true` 强制重新查询上游。上游额度请求本身为 GET，但管理端查询可能按下述规则更新本地渠道启停状态。

`GET /v1/usage` 只读取新鲜缓存，不访问上游。仅输出调用者允许访问、已启用且服务商条件仍有效的渠道；缓存须与当前渠道配置匹配；模型权限受限时，还须至少提供一个调用者允许的模型名或映射别名。智谱快照使用 `provider: "zhipu"`，两个窗口位于 `rate_limit.primary_window`、`rate_limit.secondary_window`；无符合条件的快照时不输出该渠道。

确认 `inactive` 后，管理端查询和后台刷新均可将渠道的 `enabled` 持久化为 `false`，但必须满足前述 `type=zhipu`、API Key、官方 HTTPS 条件，且模型 base URL 的路径属于：

- `/api/coding/` 开头的路径，例如 `/api/coding/paas/v4`。
- `/api/anthropic`、其尾部斜杠形式或 `/api/anthropic/` 下的子路径。

`/api/paas/v4` 等按量付费路径、OAuth、非 `zhipu` 服务商均不在自动停用范围。停用后渠道不会参与正常路由；后台刷新跳过已停用渠道，管理端仍可主动查询它的最新订阅信息。

续订成功不会自动启用渠道。符合上述 Coding Plan 范围的显式启用请求（`POST /api/admin/channels/{id}/enable`，或编辑时设置 `enabled: true`）会重新查询订阅，不使用旧缓存代替检查；确认仍未订阅时返回 `409`，查询失败时也不会完成启用。

持久化停用使用条件更新，核对渠道 `id`、`type`、`base_url`、`key`、`auth_type`、`extra`。这些值在查询期间发生变化时，放弃过期结果的停用操作、清除缓存并返回 `503`。渠道配置更新、显式启用、显式停用都会清除旧额度缓存。前端重新加载时丢弃旧快照，并防止较早请求覆盖较新的加载结果。

额度耗尽与未订阅停用是两个机制：`limit_reached=true` 会为该渠道的模型设置临时 `plan_limit` 冷却，不修改持久化 `enabled`；正常额度快照可以清除该原因的冷却。冷却时长目前优先参考 `primary` 的重置时间或剩余秒数，最多 6 小时；没有可用信息时回退为 30 秒。后台会重新查询并退避重试，不能把这理解为精确等待所有窗口中最晚的重置时间。

`inactive` 快照不当作额度耗尽或额度恢复来修改临时冷却。前端显示“未订阅”，隐藏额度进度条，不用 0% 进度条替代该状态。

依据：[后台刷新与停用逻辑](../src/rotor/services/quota_refresher.py)、[额度缓存](../src/rotor/services/usage_cache.py)、[渠道前端](../src/rotor/frontend/pages/channels.js)、[订阅状态测试](../tests/test_zhipu_subscription_state.py)。

## 验证边界

截至 2026-10-30，本文的线上响应记录仅涵盖中国站的上述未订阅业务错误。正常 `TOKENS_LIMIT`、`CREDIT_LIMIT`、时间戳映射、错误处理、服务商限制及并发防护以代码和仓库模拟测试为依据；这些测试不等于正常订阅账号的线上验证。

截至 2026-10-30，本文没有团队请求和 Z.ai 真实返回的线上验证记录。不要把个人中国站的实测结果推断为这些环境的完整行为，也不要据此承诺接口长期稳定。后续若接口改变，应先保留脱敏响应证据，再更新实现与本文档。
