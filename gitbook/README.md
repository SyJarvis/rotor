# Rotor

Rotor 是一个 API-only 的 LLM 网关。它把多个上游模型渠道统一暴露为
OpenAI Chat Completions、OpenAI Responses、OpenAI Images 和 Anthropic
Messages 兼容接口。

通过 Rotor，你可以：

- 为同一个模型配置多个上游渠道；
- 使用优先级、权重、会话亲和或自适应策略选择渠道；
- 在可重试的上游错误发生时切换到其他渠道；
- 为调用方签发独立 API Key，并限制配额和可访问渠道；
- 查看请求日志、用量、延迟和路由决策；
- 在首页查看最近请求的运行状态和近期异常，并按自然日、周或月分析用量；
- 为 MCP Client 创建可单独停用的只读 Control Key；
- 使用浏览器管理页或 HTTP 管理 API 完成配置。

## 最短路径

1. 按[安装与启动](getting-started/installation.md)运行服务。
2. 按[完成第一次请求](getting-started/quickstart.md)创建渠道、签发 Key 并发起请求。
3. 根据客户端选择[接入指南](guides/client-integrations.md)，或直接查阅[客户端 API](reference/client-api.md)。

## 支持的客户端入口

| 客户端协议 | 入口 |
| --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| OpenAI Images | `POST /v1/images/generations` |
| Anthropic Messages | `POST /anthropic/v1/messages` |

Rotor 的管理页位于 `/`，健康检查位于 `/health`。管理页聊天默认使用 Chat Completions，
也可切换到 Responses；右侧的模型、API 格式、System Prompt、temperature 和 max_tokens 按对话
保存。连接测试会按当前参数发起一次简短的真实文本生成请求，可能产生上游费用，
不验证图片或文档理解能力。

聊天支持文字、图片及 UTF-8 文本、PDF、DOCX 文档。PDF/DOCX 仅提取文字，不保留图片
或表格布局，不执行 OCR；扫描 PDF 请先转为图片。对话历史保存在当前浏览器本地，可
新建、导出 JSON 或删除，不会跨浏览器同步。它与网关请求记录的会话存储相互独立。
附件限制见 [README 的管理页聊天说明](../README.md#管理页聊天)。

## 项目边界

Rotor 管理渠道、协议转换、路由、鉴权、用量与请求记录。它不会替上游模型提供商
创建账号或 API Key，也不会保证跨协议转换能够保留某个供应商的全部私有扩展。
需要原生 Responses 或 Anthropic 特性时，应配置相应的原生协议渠道。

管理页聊天通过 OpenAI SDK 使用网关，不加载 MCP 工具。独立 Rotor MCP 服务与
Control API 保持可用；管理页聊天接口不是稳定的外部 API。
