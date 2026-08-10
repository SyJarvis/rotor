# 选择路由策略

Rotor 可以在同一模型的多个兼容渠道之间选择不同的排序策略。策略通过
`/api/admin/settings` 在运行时保存。

## 查看当前设置

```bash
curl http://127.0.0.1:8000/api/admin/settings
```

响应同时给出设置内容和设置文件路径。

## 可用策略

| 策略 | 行为 |
| --- | --- |
| `priority_weighted` | 先按优先级分层，再按权重排列；默认策略 |
| `fallback_order` | 按优先级降序、渠道 ID 升序生成确定性顺序 |
| `weighted` | 忽略优先级，按权重生成候选顺序 |
| `adaptive` | 不跨越优先级层，在层内根据成功率、延迟、负载和成本信号评分 |

成本定价当前尚未实现，因此 adaptive 的成本信号保持中性。

## 修改策略

更新时必须提交完整的 `routing` 设置对象。最简单的方式是先读取现值，再修改目标
字段。下面启用 adaptive，并保留默认权重：

```bash
curl -X PUT http://127.0.0.1:8000/api/admin/settings \
  -H "Content-Type: application/json" \
  -d '{
    "routing": {
      "strategy": "adaptive",
      "affinity_enabled": true,
      "adaptive_success_weight": 0.55,
      "adaptive_latency_weight": 0.25,
      "adaptive_cost_weight": 0.10,
      "adaptive_load_weight": 0.10,
      "adaptive_ewma_alpha": 0.20,
      "adaptive_prior_successes": 9.0,
      "adaptive_prior_failures": 1.0,
      "adaptive_latency_target_ms": 2000.0,
      "adaptive_cost_target": 0.01
    }
  }'
```

## 会话亲和

启用 `affinity_enabled` 后，携带相同 `X-Conversation-Id` 的请求会获得稳定的加权
渠道顺序。除 `weighted` 外，亲和排序仍然服从优先级边界。adaptive 使用亲和结果
作为同分渠道的决胜顺序。

策略语义和失败冷却机制见[路由与故障转移](../concepts/routing-and-fallback.md)。
