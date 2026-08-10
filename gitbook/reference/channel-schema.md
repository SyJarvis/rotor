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

未配置路径和认证方式时，Rotor 使用 provider preset 和 `protocol` 推导默认值。

## Provider 类型

adapter factory 当前注册：

```text
openai, deepseek, azure, anthropic,
moonshot, minimax, zhipu, kimi
```

`protocol=openai_responses` 会优先选择 Responses adapter，不依赖 `type` 指定一个
单独的 Responses provider。
