# 完成第一次请求

本页从一个正在运行的 Rotor 实例开始，完成渠道创建、用户 Key 签发和第一条
Chat Completions 请求。

## 1. 登录管理页面

打开 `http://127.0.0.1:8000/`，使用首次启动凭据登录：

```text
用户名：admin
密码：123456
```

按页面要求设置不少于 6 个字符的新密码。完成改密前，其他管理接口不会开放。

## 2. 创建渠道

下面示例使用一个 OpenAI 兼容上游。请替换 URL、Key 和模型名：

在管理页面的“渠道”中提交：

```json
{
    "name": "primary",
    "type": "openai",
    "key": "provider-api-key",
    "base_url": "https://provider.example/v1",
    "models": ["your-model"],
    "model_mapping": {},
    "priority": 10,
    "weight": 1,
    "enabled": true,
    "protocol": "openai"
}
```

`base_url` 是上游 API 根地址，`key` 是上游 Key。不同供应商或不同 Key 应建立为
不同渠道。

## 3. 检查模型

```bash
curl http://127.0.0.1:8000/v1/models
```

响应中应出现 `your-model`。如果没有，请检查渠道是否启用，以及模型名是否存在于
`models`。

## 4. 签发 Rotor API Key

在管理页面的“API Key”中创建名称为 `quickstart`、配额为 `1000000` 的 Key。

保存响应中的 `key`：

```bash
export ROTOR_API_KEY="sk-..."
```

## 5. 发起请求

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $ROTOR_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Conversation-Id: quickstart-1" \
  -d '{
    "model": "your-model",
    "messages": [{"role": "user", "content": "Reply with ROTOR_OK"}]
  }'
```

成功响应会携带 `X-Rotor-Channel` 和 `X-Rotor-Provider-Model` 响应头，分别指出
实际渠道和映射后的上游模型。

接下来可以[配置更多渠道](../guides/channels.md)，或[接入常用客户端](../guides/client-integrations.md)。
