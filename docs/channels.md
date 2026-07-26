# 渠道配置

每个 **channel** 描述一个上游 provider 调用入口,包含:

- `base_url` — 上游 provider 的 API 根
- `key` — 上游 provider 的 API Key
- `models` — 该渠道支持的模型列表
- `model_mapping` — 模型名映射(可选)
- `priority` — 优先级(数字越大越优先)
- `weight` — 同优先级内的权重
- `enabled` — 是否启用
- `protocol` — 协议类型(`openai` / `anthropic`)

## 创建渠道

```bash
curl -X POST http://localhost:8000/api/admin/channels \
  -H "Content-Type: application/json" \
  -d '{
    "name": "zhipu-main",
    "type": "zhipu",
    "key": "provider-api-key",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "models": ["glm-4.7"],
    "model_mapping": {},
    "priority": 10,
    "weight": 1,
    "enabled": true,
    "protocol": "openai"
  }'
```

## 路由规则

- 不同 provider、不同 key 应配置为**独立渠道**。
- 服务同一模型的多个渠道,按 **priority → weight** 进行负载均衡。
- 上游失败时按 priority 降级 fallback。

## 内置 provider 类型

- `zhipu` — 智谱 GLM
- `moonshot` — Moonshot Kimi
- `kimi` — Kimi(兼容路径)
- `minimax` — MiniMax

详细的 provider 适配与协议转换逻辑参见 → [流式请求与异步记账设计](./流式请求与异步记账设计.md)
