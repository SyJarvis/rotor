# Command Goat 订阅信息接口

本文记录 Rotor 接入 Command Code GOAT 订阅查询所需的上游接口。接口路径和调用顺序来自官方 `command-code` CLI 1.88.2，行为验证边界见文末。文中账户相关响应、时间和账务数值均为合成示例，不是实测记录；示例仅用于说明字段与行为。它们属于 `/alpha` 内部接口，公开 Provider API 文档没有承诺这些接口的稳定性。

## 地址与认证

订阅查询使用固定地址 `https://api.commandcode.ai`。不要把 `/alpha` 路径拼到模型调用地址 `https://api.commandcode.ai/provider/v1` 后面。

所有请求均为 `GET`，使用与 Provider API 相同的 API Key：

```http
Authorization: Bearer <API Key>
Accept: application/json
```

密钥过期会导致认证失败，不能据此判断订阅不存在。日志、响应缓存及文档均不应保存密钥、用户身份或组织身份信息；请求不要跟随重定向。

## 调用顺序

| 顺序 | 接口 | 查询参数 | 用途 |
|---|---|---|---|
| 1 | `/alpha/whoami` | `limits=1` | 获取当前账户及组织范围 |
| 2 | `/alpha/billing/subscriptions` | 可选 `orgId` | 查询订阅状态与计费周期 |
| 2 | `/alpha/billing/credits` | 可选 `orgId` | 查询各类余额及额度窗口，可与订阅请求并行 |
| 3 | `/alpha/usage/summary` | 可选 `orgId`、`since` | 查询使用量汇总 |

`orgId` 取自 `whoami.org.id`。若 `org` 为 `null`，省略该参数，不传字符串 `null` 或空字符串。官方 CLI 将订阅返回的 `data.currentPeriodStart` 作为汇总接口的 `since`；没有该值时省略 `since`。此时不能假定汇总一定对应当前计费周期。

下面是个人账户的请求示例。先通过环境变量注入 `API_KEY`，不要在命令中写真实密钥，也不要开启 shell 调试输出。`SINCE` 应来自刚取得的订阅响应。

```bash
: "${API_KEY:?请先设置 API_KEY 环境变量}"

curl --fail-with-body --silent --show-error --max-time 15 \
  --header "Authorization: Bearer ${API_KEY}" \
  --header 'Accept: application/json' \
  'https://api.commandcode.ai/alpha/whoami?limits=1'

curl --fail-with-body --silent --show-error --max-time 15 \
  --header "Authorization: Bearer ${API_KEY}" \
  --header 'Accept: application/json' \
  'https://api.commandcode.ai/alpha/billing/subscriptions'

curl --fail-with-body --silent --show-error --max-time 15 \
  --header "Authorization: Bearer ${API_KEY}" \
  --header 'Accept: application/json' \
  'https://api.commandcode.ai/alpha/billing/credits'

SINCE='2030-01-01T00:00:00.000Z' # 合成示例；实际取订阅的 currentPeriodStart
curl --fail-with-body --silent --show-error --max-time 15 --get \
  --header "Authorization: Bearer ${API_KEY}" \
  --header 'Accept: application/json' \
  --data-urlencode "since=${SINCE}" \
  'https://api.commandcode.ai/alpha/usage/summary'
```

组织账户在后三个请求中使用 `--get --data-urlencode "orgId=${ORG_ID}"`。`ORG_ID` 必须来自第一步响应；不能通过更换组织参数推测其他组织的订阅。

## 账户与订阅

`whoami` 实测返回 `success`、`user`、`org` 三个字段。本次个人账户的 `org` 为 `null`。只需在进程内读取组织 ID，不向 Rotor 使用量响应透传 `user` 或组织身份。

订阅响应的合成字段摘要如下，省略了其他字段；这不是完整响应或实测数据：

```json
{
  "success": true,
  "data": {
    "planId": "individual-goat",
    "status": "active",
    "currentPeriodStart": "2030-01-01T00:00:00.000Z",
    "currentPeriodEnd": "2030-02-01T00:00:00.000Z",
    "cancelAtPeriodEnd": false,
    "canceledAt": null,
    "endedAt": null
  }
}
```

| 字段 | 含义与处理 |
|---|---|
| `data.planId` | 原始计划标识，示例为 `individual-goat` |
| `data.status` | 原始订阅状态，示例为 `active`；不要把未知状态默认改成 `active` 或 `inactive` |
| `data.currentPeriodStart` | 当前计费周期开始时间，ISO 8601；用于查询本期汇总 |
| `data.currentPeriodEnd` | 当前计费周期结束时间，ISO 8601；不等于确定停用时间 |
| `data.cancelAtPeriodEnd` | 是否安排在本周期结束时取消；不能单独证明订阅已失效 |
| `data.canceledAt`、`data.endedAt` | 取消或结束时间，示例均为 `null` |

本次只验证了有效订阅。无订阅时的实际响应、其他订阅状态及组织账户尚未线上验证。缺失字段、错误响应或认证失败都不能转换成“未订阅”；对后续出现的明确空订阅响应，需要独立保留其语义，不能推断余额也为零。

## 余额与额度窗口

以下是合成的 `credits` 接口响应示例，不是实测数据：

```json
{
  "credits": {
    "belowThreshold": false,
    "creditThreshold": 0,
    "monthlyCredits": 60,
    "purchasedCredits": 0,
    "freeCredits": 0
  },
  "windowLimits": {
    "limited": true,
    "exceeded": null,
    "fiveHour": {
      "used": 0,
      "cap": 14,
      "exceeded": false,
      "resetAt": 0
    },
    "weekly": {
      "used": 0,
      "cap": 35,
      "exceeded": false,
      "resetAt": 0
    }
  },
  "sandboxAccess": true,
  "sandboxMinutes": {
    "usedMinutes": 0,
    "limitMinutes": 0,
    "overageUsd": 0,
    "resetAt": 1896134400000
  }
}
```

| 字段 | 含义与处理 |
|---|---|
| `credits.monthlyCredits` | 订阅额度剩余量，不是本期额度上限；官方 CLI 按余额展示 |
| `credits.purchasedCredits` | 购买额度剩余量 |
| `credits.freeCredits` | 免费额度剩余量 |
| `credits.belowThreshold`、`creditThreshold` | 余额阈值相关字段；不是订阅状态，不用于判断未订阅 |
| `windowLimits.limited` | 窗口限制信息；示例为 `true` 但两个窗口均未超额，不能把它当成“已耗尽” |
| `windowLimits.exceeded` | 示例为 `null`；非空值的形状和语义尚未线上验证 |
| `windowLimits.fiveHour` | 5 小时额度窗口 |
| `windowLimits.weekly` | 每周额度窗口 |
| 窗口的 `used`、`cap` | 已用 credits 与实际 credits 上限；`cap > 0` 时可计算 `used / cap × 100` |
| 窗口的 `exceeded` | 该窗口是否超额，示例均为 `false` |
| 窗口的 `resetAt` | 官方 CLI 与 `Date.now()` 直接比较，按 Unix 毫秒时间戳处理；示例中两个窗口均为 `0`，表示没有可展示的有效重置时间 |
| `sandboxAccess`、`sandboxMinutes` | 云沙箱信息，不作为模型调用订阅额度 |

每个窗口上限必须使用响应的 `cap`，不写死产品页面的宣传上限或示例数值。月度余额接口没有直接提供本期总额度，不能单凭计划名称或 `monthlyCredits` 拼出一个月度百分比。

GOAT 的 credits 是额度计量单位，不能直接标成美元。官方 Usage Limits 页面说明了 5 小时 14 credits、7 天 35 credits 与不同模型可折算使用价值的区别。合成示例使用相同的窗口上限，仅用于说明字段。窗口从首次请求开始，分别在 5 小时、7 天后重置，不按自然日或自然周重置。购买额度不计入这两个窗口，也不受窗口耗尽限制。

`resetAt` 缺失、为 `null` 或为 `0` 时，应返回未知时间，不显示 1970 年、不用“当前时间加 5 小时”补造。正数毫秒转换的依据来自官方 CLI；正数窗口重置时间的验证范围见「验证边界与参考」。

## 使用量汇总

下面为合成的计费周期响应摘要，省略 token 计数字段；它不是实测数据。假设此前本期用量发生后两个短期窗口均已重置，因此上面的窗口用量为零：

```json
{
  "totalCount": 100,
  "totalCost": 10,
  "averageCost": 0.1,
  "successRate": 100,
  "completedCount": 100,
  "failedCount": 0,
  "totalCredits": 10,
  "totalFreeCredits": 0,
  "totalMonthlyCredits": 10,
  "totalPurchasedCredits": 0,
  "periodBasis": "billing-period",
  "periodStart": "2030-01-01T00:00:00.000Z",
  "daily": []
}
```

`totalCost` 是使用成本汇总；`totalMonthlyCredits`、`totalPurchasedCredits`、`totalFreeCredits` 是按额度来源拆分的已用量，与 `credits` 接口的剩余量不同。`totalCount` 等字段为请求统计。用 `periodBasis` 和 `periodStart` 确认统计范围，不将不同周期的余额与已用量混合计算。

## 错误与渠道状态

旧密钥在 `/alpha/whoami` 返回过 HTTP `401`，错误摘要为：

```json
{
  "success": false,
  "error": {
    "code": "UNAUTHORIZED",
    "status": 401,
    "message": "Your API key has expired. Log in again to get a new key: run `cmd login`, or create a new key in Command Code Studio.",
    "docs": "https://commandcode.ai/docs/reference/errors/unauthorized"
  }
}
```

更换密钥后，四个接口均返回 `200`。这说明认证错误必须与订阅状态分开处理。

| 情况 | Rotor 接入要求 |
|---|---|
| `401`、`403` | 认证或权限错误，不标记未订阅，不把旧快照当成新结果 |
| 网络异常、超时、`429`、`5xx` | 查询失败；允许稍后重试，不伪造零额度 |
| 非 JSON、错误响应或必要字段格式错误 | 无效上游响应；不伪造有效订阅或空订阅 |
| `active` 与有效额度响应 | 展示订阅、余额、窗口数据 |
| 明确无订阅或非有效订阅状态 | 独立报告订阅状态；余额和模型访问能力需分别判断 |
| 订阅窗口耗尽 | 报告窗口状态；不能据此将整个渠道持久化停用 |

GOAT 计划额度耗尽后，购买额度仍可能支持付费调用，免费模型也可能继续可用。因此订阅状态、计划窗口限制和整个渠道的可用性是不同的信息，不能直接套用智谱 Coding Plan 的停用规则。仅凭本组查询不能证明所有模型都应进入渠道级冷却。

## Rotor Python 接口

`rotor.services.command_goat_quota.fetch_command_goat_usage(client, channel)` 使用上述四个只读接口，返回 `ChannelUsageResponse`。必须在渠道服务商中选择 **Command Code（`type: "command"`）** 并使用 API Key，地址必须为 HTTPS 的精确主机 `api.commandcode.ai`，端口为默认值或 `443`。仅填官方地址但选择 `openai`、`anthropic` 或其他服务商，不会触发 Goat 订阅查询。OAuth、名称匹配、相似域名及其他端口均不纳入。查询使用固定官方主机，不转发渠道 `extra.headers`，不跟随重定向。

同理，智谱订阅查询和缺失订阅停用只适用于明确选择 `type: "zhipu"` 且使用官方 HTTPS 地址的 API Key 渠道。服务商选择与 `protocol` 是不同配置：选择 `zhipu` 后仍可保留 `anthropic` 协议。系统不会根据域名自动更改已有渠道的服务商。管理端与 `/v1/usage` 都检查当前服务商条件，旧缓存不能绕过条件。

管理端复用 `GET /api/admin/channels/{channel_id}/oauth/usage`。路径中的 `oauth` 是既有名称，同样支持 Goat API Key。默认使用 90 秒内的缓存；`?refresh=true` 强制查询。`GET /v1/usage` 只读取新鲜缓存，不访问上游：仅输出调用者允许访问、已启用且服务商条件仍有效的渠道，缓存须与当前渠道配置匹配；模型权限受限时，还须至少提供一个调用者允许的模型名或映射别名。Goat 标识为 `provider: "command-goat"`。

| Rotor 字段 | 映射 |
|---|---|
| `plan` | 上游 `data.planId` |
| `subscription_status` | 上游 `data.status` 原始字符串；未知状态保留 |
| `allowed` | `null`，账户额度不足以推断整个渠道所有模型是否可用 |
| `limit_reached` | 管理端：存在窗口时使用窗口的明确 `exceeded` 布尔值；任一为 `true` 即为 `true`，无窗口时为 `null`；不使用含义未验证的全局 `windowLimits.exceeded`。客户端 `/v1/usage` 始终为 `null`，不表示窗口未耗尽 |
| `primary`、`secondary` | 5 小时、每周窗口的通用展示字段；正数 `cap` 才生成百分比，展示百分比最大为 100；无有效重置时间时为 `null` |
| `command_goat.subscription` | 原始计划、状态、周期和取消信息，字段名转换为 snake_case，时间保留 ISO 8601 |
| `command_goat.credits` | `monthly_remaining`、`purchased_remaining`、`free_remaining`，单位为 credits |
| `command_goat.window_limited` | 上游 `windowLimits.limited`，不等于耗尽 |
| `command_goat.five_hour`、`command_goat.weekly` | `used`、`cap`、`exceeded` 及转换为 Unix 秒的 `reset_at` |
| `command_goat.summary` | `total_cost`、`total_monthly_credits`、`total_purchased_credits`、`total_free_credits`、`total_count`、`period_start`、`period_basis` |

上表描述管理端的 `ChannelUsageResponse`；`command_goat` 不包含用户、组织或任意上游扩展字段。客户端 `/v1/usage` 只保留 `command_goat.window_limited`、`five_hour` 和 `weekly` 的 `exceeded`、`reset_at`，以及 `rate_limit.primary_window`、`rate_limit.secondary_window` 的百分比和时间字段；不返回余额、成本、原始窗口用量/上限、汇总或专属订阅详情。通用 `plan`、`subscription_status`、`allowed` 和 `fetched_at` 仍可返回。管理端保留上表完整的类型化账务数据。

实现防御性支持 `success: true` 且明确存在 `data: null` 的订阅响应，将其映射为 `subscription_status: "inactive"`、`plan: null` 和 `command_goat.subscription: null`，保留余额与汇总。该空订阅形状尚未线上验证。缺失 `data`、非成功响应、空对象、缺失计划或状态字段均报查询错误。

金额和额度必须为有限数字，不接受布尔值、数字字符串、NaN 或无穷值。窗口 `exceeded` 必须为布尔值；汇总缺少周期信息时保持未知，不补造当前周期。认证或响应格式错误映射为管理端 `502`；网络异常、超时、上游 `429`、`5xx` 映射为 `503`。错误文本固定，不透传上游响应体。失败刷新不生成新快照。

后台刷新器可以查询 Goat，但 Goat 快照只更新缓存，不新增或清除任何路由冷却，也不修改渠道 `enabled`。该接入不自动判断免费模型和付费余额的可用范围。

管理端「渠道」页已接入 Command Code 订阅面板，复用上述查询与手动刷新入口。面板显示套餐、上游订阅状态、当前周期截止和计划取消标记，以及月度、购买、免费三类剩余 credits 与 5 小时、每周窗口的已用/上限。所有额度使用 credits 单位，不显示为美元，也不将月度余额推算为进度条。只有正数窗口上限及已知用量才显示百分比，缺失或零重置时间不生成日期；未知订阅状态保留原文，未订阅仍保留已知余额和窗口。窗口耗尽提示不表示渠道已暂停，查询失败提供重试而不伪造未订阅或零额度。实现与行为测试见 [渠道页面](../src/rotor/frontend/pages/channels.js) 和 [前端测试](../tests/test_channel_oauth_frontend.mjs)。

## 验证边界与参考

已验证：个人账户、有效订阅、三个余额、两个未耗尽窗口、`resetAt=0` 及正数重置时间、按计费周期查询汇总，以及密钥过期的 `401`。

后续只读查询的 Rotor 归一化结果验证了正数窗口重置时间可从上游毫秒值转换为 Unix 秒；该记录不是上游原始 JSON。观察到 `window_limited: true`、两个窗口 `exceeded: false` 且管理端归一化 `limit_reached: false`，验证了窗口限制开启不等于额度耗尽；这不改变客户端接口将该字段设为 `null` 的约定。上文零用量和零重置值仅为合成示例，不是该记录的数值。

尚未线上验证：组织账户、空订阅、其他订阅状态、窗口耗尽及 `windowLimits.exceeded` 非空响应。实现应以保留未知状态和避免误停用为原则，并用测试覆盖这些分支；模拟测试不能替代上游真实返回的证据。

- [GOAT 计划与额度耗尽后的行为](https://commandcode.ai/docs/plans/goat#usage-beyond-plan-limits)
- [Provider API：调用地址与密钥认证](https://commandcode.ai/docs/provider)
- [Usage Limits：credits 单位、窗口起点与购买额度规则](https://commandcode.ai/docs/resources/usage-limits)
- [官方 CLI 1.88.2 发布包](https://registry.npmjs.org/command-code/-/command-code-1.88.2.tgz)：`dist/cli.mjs` 中的 `fetchUsageData`、`fetchUsageWhoami`、`fetchUsageCredits`、`fetchUsageSubscription`、`fetchUsageSummary`、`projectUsageView`、`WindowLimitMeter`
