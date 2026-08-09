# 配置与维护渠道

Channel 表示一个具体的上游调用入口：一个供应商地址、一把上游 Key、一组模型和
一套路由属性。

## 创建渠道

可以在浏览器管理页中添加渠道，也可以调用 `POST /api/admin/channels`：

```json
{
  "name": "zhipu-main",
  "type": "zhipu",
  "key": "provider-api-key",
  "base_url": "https://open.bigmodel.cn/api/paas/v4",
  "models": ["glm-4.7"],
  "model_mapping": {},
  "priority": 10,
  "weight": 1,
  "enabled": true,
  "test_only": false,
  "protocol": "openai",
  "extra": {}
}
```

字段的完整约束见 [Channel 字段](../reference/channel-schema.md)。

## 选择 provider 和 protocol

内置 provider 类型包括 `openai`、`deepseek`、`azure`、`anthropic`、
`moonshot`、`minimax`、`zhipu` 和 `kimi`。

`protocol` 描述上游使用的线协议：

- `openai`：上游接受 Chat Completions；
- `openai_responses`：上游原生接受 Responses；
- `anthropic`：上游原生接受 Anthropic Messages。

客户端协议与上游协议可以不同。Rotor 会在支持的组合间转换，但供应商私有字段
只有在原生转发时才能可靠保留。

## 模型名映射

`models` 是客户端可见的模型名。需要向上游发送另一个名字时，使用
`model_mapping`：

```json
{
  "models": ["coding-model"],
  "model_mapping": {
    "coding-model": "provider-model-2026-07"
  }
}
```

客户端请求 `coding-model`，Rotor 向该渠道发送 `provider-model-2026-07`。

## 探测和测试

获取供应商预设：

```bash
curl http://127.0.0.1:8000/api/admin/channels/presets
```

普通渠道连通性测试调用上游 `GET /models`，不生成模型内容。显式的能力测试可能
产生上游费用：

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/admin/channels/1/test?capability=function_call&test_model=your-model"
```

也可以使用 `POST /api/admin/channels/probe-models` 在保存渠道前获取模型列表。

## 启停渠道

```bash
curl -X POST http://127.0.0.1:8000/api/admin/channels/1/disable
curl -X POST http://127.0.0.1:8000/api/admin/channels/1/enable
```

被禁用的渠道不会进入候选集。删除渠道前，应确认没有原生 Responses 对象仍依赖
该渠道的持久化归属信息。

下一步：[选择路由策略](routing.md)。
