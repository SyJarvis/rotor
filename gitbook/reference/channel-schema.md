# Channel 字段

Channel 是路由和上游适配的主要配置对象。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 必填 | 渠道名称，1–100 字符 |
| `type` | string | 必填 | provider adapter 类型 |
| `key` | string | 必填 | 上游供应商 API Key |
| `base_url` | string | 必填 | 上游 API 根地址 |
| `models` | string[] | `[]` | 客户端可请求的模型 |
| `model_mapping` | object | `{}` | 客户端模型到上游模型的映射 |
| `priority` | integer | `1` | 管理优先级，越大越优先 |
| `weight` | integer | `1` | 加权排序权重，最小为 1 |
| `enabled` | boolean | `true` | 是否参与模型发现和路由 |
| `test_only` | boolean | `false` | 测试标记 |
| `protocol` | string | `openai` | `openai`、`openai_responses` 或 `anthropic` |
| `rpm_limit` | integer/null | `null` | 预留的每分钟请求限制字段 |
| `tpm_limit` | integer/null | `null` | 预留的每分钟 Token 限制字段 |
| `extra` | object | `{}` | provider 和路由扩展配置 |

当前代码保存 `rpm_limit` 和 `tpm_limit`，但没有证据表明请求路径会强制执行这两个
限制；不要把它们当作已生效的限流保证。

## 常用 extra 字段

| 字段 | 用途 |
| --- | --- |
| `models_path` | 覆盖模型发现路径 |
| `request_path` | 覆盖生成请求路径 |
| `images_path` | 覆盖 Images 生成路径 |
| `auth_type` | 选择 `bearer`、`x-api-key` 或 `api-key` |
| `headers` | 添加上游请求头 |
| `capabilities` | 显式声明渠道能力集合 |
| `fallback_cooldown_seconds` | 可重试失败后的冷却时间 |
| `cache_scope` | 可能共享 provider cache 的资源范围 ID |
| `capacity_scope` | 共享 RPM、TPM、并发或账号额度的范围 ID |
| `billing_scope` | 共享余额、套餐或账单的范围 ID |
| `tariffs` | 按模型配置版本化 Token 费率与每日峰谷时段 |
| `session_lease_idle_ttl_seconds` | 覆盖该 Channel 的 Session Lease 空闲 TTL，60–86400 秒 |
| `session_lease_idle_ttl_by_model` | 按 logical model 或 `*` 覆盖 Session Lease 空闲 TTL |

未配置路径和认证方式时，Rotor 使用 provider preset 和 `protocol` 推导默认值。

## Provider 类型

adapter factory 当前注册：

```text
openai, deepseek, azure, anthropic,
moonshot, minimax, zhipu, kimi
```

`protocol=openai_responses` 会优先选择 Responses adapter，不依赖 `type` 指定一个
单独的 Responses provider。

## 资源 Scope 配置

只有明确知道多个 Channel 属于同一 provider 资源边界时，才在 `extra` 中配置相同
的 scope ID：

```json
{
  "cache_scope": "provider/account-a/cache",
  "capacity_scope": "provider/account-a/capacity",
  "billing_scope": "provider/account-a/billing"
}
```

每种 scope 独立比较；两个 Channel 只有对应字段使用完全相同的 ID，才被视为共享
该类资源。ID 必须是 1–100 个 ASCII 字母、数字或 `._:/-`，首字符必须是字母或数字。

未配置时，生效值为 `channel:<id>`，即保守地按 Channel 隔离。旧数据库中存在非法
值时也回退到该默认值，避免误把无关账号合并。修改 scope 只影响之后的请求；每条
RequestLog、UsageLedger 和 routing decision 都保存当时生效的 scope。

`cache_scope` 只表示 provider 文档或运营信息认为缓存可能共享，不保证某次请求一定
命中；Rotor 不会据此迁移或重建 KV Cache。scope ID 是非敏感机器标识，不应填写
API Key、账号密码或其他秘密。

## Tariff 配置

Rotor 不内置供应商价格。需要费用事实时，在 Channel 的 `extra.tariffs` 中显式配置，
费率单位均为“每百万 Token”：

```json
{
  "tariffs": {
    "provider-model-name": {
      "version": "provider-2026-08-19",
      "currency": "USD",
      "timezone": "Asia/Shanghai",
      "rates": {
        "input": 2.0,
        "cache_read": 0.2,
        "cache_write": 2.5,
        "cache_write_5m": 2.5,
        "cache_write_1h": 4.0,
        "output": 12.0
      },
      "periods": [
        {
          "name": "off_peak",
          "start": "00:30",
          "end": "08:30",
          "rates": {
            "input": 1.0,
            "cache_read": 0.1,
            "output": 6.0
          }
        }
      ]
    }
  }
}
```

Tariff key 依次匹配映射后的 provider model、客户端请求的 logical model 和 `*`。
`version` 与 `currency` 必填；`timezone` 默认为 `UTC`，必须是 IANA 时区名。
`periods` 使用左闭右开的每日时间段，支持跨午夜，按配置顺序采用第一个匹配项；
时段中的 `rates` 只覆盖列出的字段，其余继承默认费率。

`cache_write_5m` / `cache_write_1h` 未单独配置时会回退到 `cache_write`。某个实际使用
的 Token 桶缺少费率时，该请求标为 `incomplete_tariff`，不会把缺失费率当作零。
计价时刻固定为实际成功上游 attempt 的开始时间，避免 fallback 或长流式请求跨过
峰谷边界后按完成时刻误计；每条记录同时保存 tariff 快照，因此后续修改 Channel
配置不会改写历史费用。

## Session Lease TTL 配置

```json
{
  "session_lease_idle_ttl_seconds": 600,
  "session_lease_idle_ttl_by_model": {
    "coding-model": 3600,
    "*": 900
  }
}
```

匹配顺序是 logical model、`*`、Channel 默认值、全局设置。非法值保守回退到下一层。
该 TTL 只决定 Rotor 何时允许 Session 重新选路，不推断或延长 provider KV Cache。
