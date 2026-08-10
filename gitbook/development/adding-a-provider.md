# 增加 Provider 适配

只有当新供应商不能由已有 OpenAI、Anthropic 或 Responses 协议配置表达时，才需要
新增 adapter。标准兼容上游应优先使用 Channel 的 `base_url`、`protocol` 和
`extra` 配置。

## 1. 判断复用路径

- OpenAI Chat 兼容：使用 `type=openai` 和 `protocol=openai`；
- Anthropic Messages 兼容：使用 `type=anthropic` 和 `protocol=anthropic`；
- 原生 Responses：使用 `protocol=openai_responses`；
- 仅路径或鉴权不同：优先使用 provider preset 或 `extra` 覆盖。

## 2. 实现 adapter

自定义 adapter 继承 `BaseAdapter` 或现有兼容基类，并实现：

```text
get_request_url
setup_request_headers
convert_request
convert_response
stream_convert_response
```

模型名应通过 `map_model_name` 处理。上游 URL 应复用 Channel path 选项，认证头应
避免在日志和错误中泄露。

## 3. 注册 provider

在 `AdapterFactory._adapters` 中注册稳定的 `type` 名称。不要注册缺少必要认证或
签名实现的 provider；例如当前 Bedrock 没有 SigV4 支持，因此明确保持未注册。

如需在管理页展示默认 URL、路径和认证类型，同时更新
`rotor.channels.presets.PROVIDER_PRESETS`。

## 4. 增加测试

至少覆盖：

- factory 能创建正确 adapter；
- URL 拼接不会重复路径；
- 认证头和扩展头正确；
- 模型映射生效；
- 非流式响应转换；
- SSE 事件和尾部 usage；
- 上游错误正文被读取、清理和关闭；
- 工具调用在请求和响应方向保持结构。

如果 provider 只是标准协议预设，测试 preset 和通用 adapter 即可，不要复制整套
协议转换代码。
