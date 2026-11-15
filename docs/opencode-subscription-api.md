# OpenCode Go 订阅信息接口

本文记录 OpenCode Go 的只读查询接口及 Rotor Python 接入行为。上游事实依据包括官方 OpenCode v2.2.2 源码、2026-10-30 官方控制台公开 JavaScript 资源及同日 API Key 查询记录。

文中账户相关响应、时间和账务数值均为合成示例，不是实测记录；实测行为和未验证范围另行注明。这些接口由官方客户端或控制台使用，但不应视为公开、稳定的订阅 API。Rotor 独立校验每个响应，保留部分成功信息，不把权限错误、额度耗尽与没有订阅混为一谈。

## 地址、认证与查询范围

所有查询固定使用 origin `https://opencode.ai`，均为 `GET`，没有必需的查询参数或请求体：

| 完整路径 | 用途 | 2026-10-30 验证结果 |
|---|---|---|
| `/zen/go/v1/usage` | Go 的 5 小时、周、月窗口百分比与限制状态 | 一个 API Key 返回 `200`；另一个 Key 返回 `401` |
| `/console/api/go/status` | Go 产品、续订设置、Extra Usage、付费访问周期及原始窗口额度 | 第一个 Key 返回 `200`，有订阅对象 |
| `/console/api/billing/status` | 工作区余额与计费方式 | 第一个 Key 返回 `403`，未读取响应体；不能据此判断余额为零 |

使用模型调用的 API Key，并发送：

```http
Authorization: Bearer <API Key>
Accept: application/json
```

不要把查询路径追加到模型地址 `/zen/go/v1` 后面，也不要将请求重定向到其他主机。Rotor 采用 15 秒请求超时、`follow_redirects=False`，不转发渠道的任意 `extra.headers`，也不输出上游身份、支付标识或原始错误体。

官方 API Key 文档说明，每个 Key 绑定一个工作区，不要求工作区请求头。上述线上成功查询只使用 Bearer Key，没有推测组织 ID。官方 v2.2.2 CLI 的登录连接另有 `credential.metadata.orgID`：查询 Go 时发送 `x-opencode-org-id`，查询 Console API 时发送 `x-org-id`。这两个头不同；CLI 登录元数据不能凭空从 Rotor 渠道名或其他响应字段生成。当前 API Key 接入不增加组织头，也不接入 OpenCode OAuth。

以下命令只演示三个只读请求。通过环境变量注入密钥，不把真实密钥写入文档、命令文件或日志，不开启 shell 调试。没有 `--location`，因此不会跟随重定向：

```bash
: "${API_KEY:?请先设置 API_KEY 环境变量}"

curl --fail-with-body --silent --show-error --max-time 15 \
  --header "Authorization: Bearer ${API_KEY}" \
  --header 'Accept: application/json' \
  'https://opencode.ai/zen/go/v1/usage'

curl --fail-with-body --silent --show-error --max-time 15 \
  --header "Authorization: Bearer ${API_KEY}" \
  --header 'Accept: application/json' \
  'https://opencode.ai/console/api/go/status'

curl --fail-with-body --silent --show-error --max-time 15 \
  --header "Authorization: Bearer ${API_KEY}" \
  --header 'Accept: application/json' \
  'https://opencode.ai/console/api/billing/status'
```

## Go 百分比窗口

`GET /zen/go/v1/usage` 的合成 JSON 示例如下，没有订阅产品、余额、Extra Usage 或用户身份字段；示例查询时刻假定为 2030-01-08 06:00 UTC：

```json
{
  "usage": {
    "rolling": {
      "status": "ok",
      "percent": 0,
      "resetsAt": "2030-01-08T11:00:00.000Z"
    },
    "weekly": {
      "status": "ok",
      "percent": 0,
      "resetsAt": "2030-01-14T00:00:00.000Z"
    },
    "monthly": {
      "status": "ok",
      "percent": 0,
      "resetsAt": "2030-02-01T00:00:00.000Z"
    }
  }
}
```

| 字段 | 含义 |
|---|---|
| `usage.rolling` | 5 小时窗口 |
| `usage.weekly` | 每周窗口 |
| `usage.monthly` | 月度额度窗口 |
| `status` | 官方客户端识别 `ok`、`rate-limited`；后者代表该 Go 窗口受限 |
| `percent` | 已用百分比，非剩余百分比；没有原始金额或金额上限 |
| `resetsAt` | ISO 8601 窗口重置时间；不是订阅到期时间 |

官方 v2.2.2 客户端按上述结构读取三窗口，将 `status == "rate-limited"` 转成 `limited`，将时间转为 Unix 毫秒。Rotor 统一转为 **Unix 秒**，不直接复用 CLI 的毫秒数值。

零用量不等于窗口已经开始。在合成示例中，`rolling.resetsAt` 是假定查询时刻加 5 小时，而订阅接口示例的五小时 `startsAt=null、resetsAt=null`。实测也观察到 usage 返回重置时间而订阅接口的五小时时间为空。因此需保留两个接口各自的值与来源：可以标注 usage 接口返回的重置时间，但不能将其填入订阅接口的空值，也不能据此证明一个真实使用窗口已经启动。

## Go 订阅、访问周期与原始额度

`GET /console/api/go/status` 的成功响应为订阅对象或 JSON `null`。当前官方控制台 schema 允许 `null`；截至 2026-10-30，本文只记录了非空订阅的线上成功查询，未验证真实无订阅响应。

下面是**合成的字段摘要，不是完整响应或实测数据**；不包含用户、支付方法、支付尝试及其他身份字段：

```json
{
  "product": "go",
  "renewalProduct": "go",
  "useBalance": false,
  "cancelAtPeriodEnd": false,
  "renewalPending": false,
  "resumability": "renewing",
  "access": {
    "startsAt": "2030-01-01T00:00:00.000Z",
    "endsAt": "2030-02-01T00:00:00.000Z",
    "cancelAtPeriodEnd": false,
    "meters": {
      "fiveHour": {
        "startsAt": null,
        "resetsAt": null,
        "limitMicroCents": "1000000000",
        "usedMicroCents": "0"
      },
      "week": {
        "startsAt": "2030-01-07T00:00:00.000Z",
        "resetsAt": "2030-01-14T00:00:00.000Z",
        "limitMicroCents": "2500000000",
        "usedMicroCents": "0"
      },
      "month": {
        "resetsAt": "2030-02-01T00:00:00.000Z",
        "limitMicroCents": "5000000000",
        "usedMicroCents": "0"
      }
    }
  }
}
```

| 字段 | 含义与 Rotor 处理 |
|---|---|
| `product`、`renewalProduct` | 当前与续订产品；当前官方 schema 为 `go`、`go-plus`、`go-teams`。Rotor 保留非空原始字符串，不按产品名硬编码额度 |
| `useBalance` | Extra Usage 开关；为真时允许窗口耗尽后使用按量余额，不证明余额充足或所有模型可用 |
| `cancelAtPeriodEnd` | 续订取消设置，不代表当前付费访问已经结束 |
| `renewalPending` | 续订处理标记，不单独决定当前访问是否有效 |
| `resumability` | 可选续订/恢复状态；当前 schema 包括 `renewing`、`resumable`、`needs-payment-method`、`access-ended`、`revoked`。Rotor 保留未知字符串，不默认解释为未订阅 |
| `access` | 当前付费访问段，可为 `null`；非空时包含实际开始/结束时间及三个额度窗口 |
| `access.startsAt`、`endsAt` | 当前付费访问段的 ISO 8601 起止时间；`endsAt` 不保证未来不续订 |
| `access.cancelAtPeriodEnd` | 该访问段的取消标记，独立保留，不覆盖外层同名字段 |
| `access.meters.fiveHour` | 五小时原始额度；`startsAt`、`resetsAt` 均允许为 `null` |
| `access.meters.week` | 周额度；有开始、重置时间 |
| `access.meters.month` | 月额度；有重置时间，没有单独提供窗口开始字段 |
| `limitMicroCents`、`usedMicroCents` | 十进制整数字符串，分别为额度上限、已用量；不能转换成充值余额 |

`1 USD = 100,000,000 microcents`。例如 `"1000000000"` 对应 10 USD 的额度计量。金额保留字符串可避免二进制浮点或 JavaScript 大整数精度损失；需要显示美元时使用十进制运算除以 `100000000`。Go 各模型的额度消耗折算不同，不能把这些计量值直接解释成每个模型都能消费的等额现金余额。

额度以响应值为准，不把合成样例的 10/25/50 或产品宣传值写成上限。原始额度必须是非负十进制整数字符串；不能接受布尔值、浮点数、NaN 或用错误字段补零。上限为零时不计算百分比。日期须包含时区且可解析，`access.endsAt` 必须晚于 `startsAt`；明确允许的 `null` 原样保留，不补造时间。

`access`、五小时窗口的 `startsAt/resetsAt`、余额接口的 `creditLimitMicroCents` 都是必需但允许显式 `null` 的字段。缺失这些字段属于无效响应，不能当成合法空值；`resumability` 是可省略字段。

月窗口的 `resetsAt` 与付费访问段的 `endsAt` 在样例中相同，但字段含义不同，不能用前者替代缺失的后者。

## 工作区余额与权限

`GET /console/api/billing/status` 的字段来自 2026-10-30 官方控制台公开 schema。**本文没有该接口成功响应的线上样例**；已记录的请求为 HTTP `403`，响应体未读取。

| 字段 | schema 与用途 |
|---|---|
| `billingMode` | 工作区计费方式字符串；保留原始值，不据此猜余额 |
| `mode` | 当前 schema 为 `pay-as-you-go` 或 `invoiceable`；Rotor 保留未知非空字符串 |
| `balanceMicroCents` | 账本余额的有符号十进制整数字符串；不要强行限制为非负或归零 |
| `availableMicroCents` | 可用金额的有符号十进制整数字符串；与账本余额分开保留 |
| `creditLimitMicroCents` | 非负十进制整数字符串或 `null`，不等同订阅额度 |
| `canPurchaseCredits` | 能否购买额度的布尔值 |
| `canEnableAutoRecharge` | 能否启用自动充值的布尔值 |

官方文档为 API Key 区分 Models、Usage、Billing 等权限；Key 的权限还受成员角色限制。一个 Key 可以成功读 Go 用量和订阅，却无法读工作区余额。`403` 应表示该来源不可读取，不能转成 `balance=0`、`useBalance=false` 或“无订阅”。不要求用户为查询额度提供浏览器 cookie，也不调用 support/admin 专用接口。

## CLI 本地 API 与云端 API 的区别

官方 `/api/experimental/usage/limits` 属于 OpenCode CLI 实例服务。它从当前 location 的登录连接取得凭据与组织元数据，聚合云端 Go 用量和 `/console/api/policies/current-budgets`，输出 `UsageLimits.Info`。其中的 budgets 是成员预算，不是工作区钱包余额。

该本地接口的 `Go.Meter` 为 `percent、limited、resetsAt`，时间单位是毫秒；云端 `/zen/go/v1/usage` 则为 `percent、status、resetsAt`，时间是 ISO 字符串。Rotor 直接调用本文列出的三个云端接口，不依赖本机安装或登录 OpenCode CLI。

## Rotor Python 接入条件

`rotor.services.opencode_quota.fetch_opencode_usage(client, channel)` 返回 `ChannelUsageResponse`，接入条件如下：

- 必须显式选择服务商 **OpenCode（`type: "opencode"`）**，并使用 `auth_type: "api_key"`。
- `base_url` 必须为 HTTPS 的精确主机 `opencode.ai`，默认端口或 `443`，无 userinfo、查询参数或 fragment；路径只能是 `/zen/go/v1`，允许一个末尾斜线。
- 官方地址配合 `openai`、`deepseek`、`anthropic` 等其他服务商不触发查询；`/zen/v1` 等 Zen 按量路径不触发 Go 查询。名称或域名子串匹配不成立。本方案仅覆盖现有 `/zen/go/v1` 渠道，其他 Go 新推理路径未纳入，不推测扩大匹配范围。
- 新增动态 provider preset：`opencode`，默认 `https://opencode.ai/zen/go/v1`、`protocol: "openai"`、Bearer、`/models`、`/chat/completions`；adapter 注册复用现有 `OpenAIAdapter`，不另建空适配器。已有显式 `openai_responses` 等协议配置可继续按协议选适配器，不自动改写渠道。
- 管理端在读取渠道缓存前检查当前服务商条件；`/v1/usage` 读取新鲜缓存集合后，再按调用者权限及渠道当前服务商过滤输出。变更渠道配置使旧缓存失效，不能靠旧缓存绕过服务商选择。不自动迁移真实渠道的 type、协议或启停状态。

管理端复用 `GET /api/admin/channels/{channel_id}/oauth/usage`，其中 `oauth` 是既有路径名称；默认读取 90 秒内缓存，`?refresh=true` 强制新查询。`GET /v1/usage` 仅输出调用者允许访问、已启用且服务商条件仍有效的渠道新鲜快照；模型权限受限时，还须至少提供一个调用者允许的模型名或映射别名。OpenCode 标识为 `provider: "opencode"`；该接口不请求上游，配置与缓存不匹配时不输出旧快照。后台刷新与管理端采用同一解析和部分成功规则。

实现位置：[OpenCode 查询与解析](../src/rotor/services/opencode_quota.py)、[通用 schema](../src/rotor/schemas/channel.py)、[管理端](../src/rotor/api/admin/channels.py)、[客户端使用量接口](../src/rotor/api/v1/usage.py)、[缓存](../src/rotor/services/usage_cache.py)、[刷新器](../src/rotor/services/quota_refresher.py)、[服务商预设](../src/rotor/channels/presets.py)、[适配器工厂](../src/rotor/adapters/factory.py)。

## 类型化响应

以下描述管理端 `ChannelUsageResponse`；其 `opencode` 是可选的 OpenCode 专属对象。usage 与 subscription 中的窗口分开存储，避免混淆不同来源的重置语义。客户端 `/v1/usage` 的精简字段另见本节末尾。

| 字段 | 结构/映射 |
|---|---|
| `opencode.sources` | 固定 `usage、subscription、billing` 三个来源状态，枚举见下一节 |
| `opencode.usage` | 可空；非空含 `five_hour、weekly、monthly`，每个含 `status、used_percent、reset_at` |
| `opencode.subscription` | 可空；非空含 `product、renewal_product、use_balance、cancel_at_period_end、renewal_pending、resumability、access` |
| `opencode.subscription.access` | 可空；非空含 `starts_at、ends_at、cancel_at_period_end、five_hour、weekly、monthly` |
| access 的三个窗口 | `used_microcents、limit_microcents、starts_at、reset_at`；保留字符串金额，月窗口未提供的 `starts_at` 为 `null` |
| `opencode.billing` | 可空；上述七个余额字段转换为 snake_case，金额仍为字符串，无重复 USD 浮点字段 |
| `plan` | 仅从成功订阅对象的 `product` 取得；usage 成功不能推测具体产品 |
| `subscription_status` | 仅根据成功订阅查询及付费访问段推导，规则见下文 |
| `allowed` | 始终为 `null`，这些信息不能确定整个渠道所有模型是否可用 |
| `limit_reached` | 管理端仅来自成功 usage 响应的窗口 `status`；任一 `rate-limited` 为 `true`，三个均为 `ok` 则为 `false`；usage 失败为 `null`。客户端 `/v1/usage` 始终为 `null`，不表示窗口未耗尽 |
| `primary、secondary` | 仅映射成功 usage 响应的五小时、周窗口，时长分别为 `18000、604800` 秒；usage 失败时均为 `null`，原始金额窗口仍保留在 subscription 内 |
| `fetched_at` | 当前查询完成时间，Unix 秒；缓存命中保留原时间 |

所有 Rotor 时间字段统一为可空的 Unix 秒，允许亚秒精度。通用 `reset_after_seconds=max(0, round(reset_at-fetched_at))`；没有有效时间时为 `null`。不为月窗口设定固定 30 天时长，不从金额窗口猜测上游 `rate-limited` 状态。usage 必须有三个完整窗口、受支持的 `status`、有限的 0–100 数值百分比及有效日期；非法来源整体记为 `invalid_response`，不伪造缺失窗口。

`subscription_status` 是 Rotor 的派生访问状态，**不是上游返回的 status 字段**，在本次查询时刻 `fetched_at` 计算：

| 成功订阅查询的结果 | 派生状态 |
|---|---|
| JSON `null` | `inactive`：该工作区没有可返回的 Go 订阅记录；不推断为 free |
| 对象且 `access=null` | `inactive`：没有当前返回的付费访问段；仍保留产品和续订设置 |
| `fetched_at < access.startsAt` | `pending` |
| `access.startsAt <= fetched_at < access.endsAt` | `active` |
| `fetched_at >= access.endsAt` | `expired`：返回的付费访问段已结束，不保证不会续订 |
| 订阅查询失败或形状无效 | `null`：未知，不是 `inactive` |

未知 `resumability`、`cancelAtPeriodEnd=true` 或 `renewalPending=true` 不覆盖有效期间判定。状态是快照时刻的结果，读取缓存不会重新宣称发生了上游查询。

下面是 **Rotor 部分成功响应的结构示意**，仅展开来源状态；`usage` 和 `subscription` 数据仍按上表保留，不能用该示意替代完整模型样例：

```json
{
  "sources": {
    "usage": "ok",
    "subscription": "ok",
    "billing": "forbidden"
  },
  "billing": null
}
```

不透传订阅者 ID、组织身份、邮箱、支付方法 ID、支付尝试 ID、未付款成员清单或任意额外上游字段。未知但合法的产品/续订状态字符串可以保留；错误文本使用固定的安全说明。

客户端 `/v1/usage` 保留通用 `plan`、`subscription_status`、`allowed`、`fetched_at` 与 `rate_limit.primary_window`、`secondary_window`；`opencode` 专属对象仅保留 `usage` 的五小时、周、月窗口（各含 `status`、`used_percent`、`reset_at`），usage 不可用时为 `null`。不暴露 `sources`、专属订阅对象、原始金额窗口或 `billing`；这些管理端详情仍然保留。

## 部分成功、错误与缓存

三个来源独立请求、独立校验。同一次查询不得把上一次缓存的数据混入新结果；某来源失败就将其数据设为 `null`，通过来源状态说明原因。

| `sources` 枚举 | 含义 |
|---|---|
| `ok` | HTTP 成功且符合该接口 schema；订阅的成功 JSON `null` 也属于 `ok` |
| `unauthorized` | HTTP `401`，认证失败；不代表无订阅 |
| `forbidden` | HTTP `403`，访问被拒绝；不可猜测是权限、角色还是订阅原因 |
| `unavailable` | 网络/超时异常、HTTP `429` 或 `5xx` |
| `invalid_response` | 成功 HTTP 返回非 JSON、错误体、错误结构或非法必要字段 |
| `error` | 其他非成功 HTTP，包括不跟随的重定向 |

- **usage 或 subscription 至少一个来源为 `ok`**：管理端返回 `200`，缓存本次完整或部分成功快照。另一来源及 billing 失败都不能丢掉已经成功的字段。
- usage 和 subscription 都失败：整体查询失败，即使 billing 成功也不生成新的 Go 订阅快照。两者均为 `unavailable` 时映射管理端 `503`；否则映射 `502`，使用固定安全错误说明。
- usage `200`、subscription `403`：保留百分比窗口，`subscription_status=null`、`plan=null`，不编造 active/free。
- usage `403`、subscription `200` 且 JSON `null`：返回 `200` 部分结果、`subscription_status=inactive`；`limit_reached=null`，不据此停用渠道。
- subscription 成功、billing `403`：保留产品、周期、原始窗口和 `use_balance`，`billing=null`、来源为 `forbidden`，不把余额填零。

强制刷新失败必须返回错误，不能把旧缓存当作本次成功结果，也不能更新旧快照的 `fetched_at`。既有快照仅可在其原 90 秒有效期内通过普通缓存读取，不能因失败延长有效期。来源状态与数据整体缓存；`/v1/usage` 仅输出上文所述精简字段及原始 `fetched_at`，并遵守渠道、模型权限与服务商过滤。

OpenCode 快照**仅用于查询和展示**。后台/管理端不得据其修改 `enabled`、自动启用渠道、创建或清除任何渠道级 cooldown，包括已有 `plan_limit` 或临时错误冷却。Go 窗口耗尽后，Extra Usage 和免费模型可能仍允许部分调用；余额查询成功也不能保证所有模型可用。

## 验证边界与固定来源

截至 2026-10-30，线上记录覆盖：一个 Key 的三个 usage 窗口 `200`、同 Key 的有效 Go 订阅 `200` 与五小时空时间、billing `403`，以及另一 Key 的 usage `401`。没有验证真实无订阅、过期/未来访问段、耗尽窗口、Go Plus/Teams、Extra Usage 开启、余额成功响应、其他角色或组织头行为。上述分支的 schema/实现规则不能冒充线上实测。

固定版源码为 **v2.2.2，commit `098aa7fd6e02b03d6fc410f0b052038c7d4c5e81`**：

- [CLI 云端用量请求与解析](https://github.com/anomalyco/opencode/blob/098aa7fd6e02b03d6fc410f0b052038c7d4c5e81/packages/core/src/usage-limits.ts)：`readGo`、`readBudgets`。
- [Console 地址与认证辅助](https://github.com/anomalyco/opencode/blob/098aa7fd6e02b03d6fc410f0b052038c7d4c5e81/packages/core/src/console-access.ts)：默认 `/console` 地址及组织头。
- [CLI 本地 API 路由](https://github.com/anomalyco/opencode/blob/098aa7fd6e02b03d6fc410f0b052038c7d4c5e81/packages/protocol/src/groups/usage.ts)、[本地响应 schema](https://github.com/anomalyco/opencode/blob/098aa7fd6e02b03d6fc410f0b052038c7d4c5e81/packages/schema/src/usage-limits.ts)。
- [同版本 API Key 与权限文档](https://github.com/anomalyco/opencode/blob/098aa7fd6e02b03d6fc410f0b052038c7d4c5e81/services/www/src/docs/content/console/api/index.mdx)。
- [官方 Go 文档](https://opencode.ai/v2/docs/console/go/)：Extra Usage、免费模型及额度计量说明；产品额度可能变化。

当前公开源码树没有旧版云端 Console 订阅 handler，因此订阅/余额的路由与 schema 另以 2026-10-30 官方公开控制台资源为依据。资源文件名和 SHA256 固定其已检查内容，不能视为稳定 API 版本：

| 官方资源 | SHA256 | 用途 |
|---|---|---|
| [index-BC8ksu1-.js](https://opencode.ai/console/assets/index-BC8ksu1-.js) | `b4e44840aadafde7bc3f2a112ce5339cd9f6a79fbbddff23673922bd80df1ac2` | `/console` base、Bearer WorkspaceMiddleware、billing 路由、金额与订阅 schema |
| [standing-Ew957xd2.js](https://opencode.ai/console/assets/standing-Ew957xd2.js) | `961ea31d18e819c329611e7515fe1bf40c8dc1af64e57f0d424dad6e40a2860f` | 普通工作区 Go `GET /api/go/status` 与查询调用；与 support 路径区分 |

[OpenCode 测试](../tests/test_opencode_quota.py) 使用 MockTransport 和内存数据库覆盖来源组合、严格类型校验、空订阅、日期边界、缓存、权限、服务商匹配及渠道状态保持。余额成功等未实测分支使用明确标注的模拟 fixture；测试通过不能替代线上验证。
