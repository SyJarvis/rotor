# 选择路由策略

Rotor 可以在同一模型的多个兼容渠道之间选择不同的排序策略。策略通过
`/api/admin/settings` 在运行时保存。

## 查看当前设置

下面的命令假定已按[管理 API 认证](../reference/admin-api.md#认证端点)建立 Session。

```bash
curl -b "$ROTOR_ADMIN_COOKIE_JAR" \
  http://127.0.0.1:8000/api/admin/settings
```

响应同时给出设置内容和设置文件路径。

## 可用策略

| 策略 | 行为 |
| --- | --- |
| `priority_weighted` | 先按优先级分层，再按权重排列；默认策略 |
| `fallback_order` | 按优先级降序、渠道 ID 升序生成确定性顺序 |
| `weighted` | 忽略优先级，按权重生成候选顺序 |
| `adaptive` | 不跨越优先级层，在层内根据成功率、延迟、负载和成本信号评分 |

排序之后还有一层**协议亲和**软分层：`protocol_affinity_enabled`（默认开启）把与
请求同协议族的渠道排在需要协议转换的渠道之前，组内保持策略顺序。它不替代优先级、
权重或 adaptive；关掉它会保留策略排序与租约置顶。需要原生 Responses 或 Anthropic
语义的请求由能力过滤（硬条件）排除不兼容渠道，不受该开关影响。

Tariff 与费用事实已经可以记录，但尚未经过 Session Lease 和真实流量验证，因此
adaptive 的成本信号仍保持中性，不会因为峰谷价逐请求切换渠道。

## 修改策略

更新时必须提交完整的 `routing` 设置对象。最简单的方式是先读取现值，再修改目标
字段。下面启用 adaptive，并保留默认权重：

```bash
curl -X PUT -b "$ROTOR_ADMIN_COOKIE_JAR" \
  -H "X-CSRF-Token: $ROTOR_ADMIN_CSRF" \
  http://127.0.0.1:8000/api/admin/settings \
  -H "Content-Type: application/json" \
  -d '{
    "routing": {
      "strategy": "adaptive",
      "affinity_enabled": true,
      "session_lease_enabled": true,
      "session_lease_idle_ttl_seconds": 1800,
      "protocol_affinity_enabled": true,
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

启用 `affinity_enabled` 后，携带相同稳定 Session ID 的请求会获得稳定的加权渠道顺序。
Session 可以来自显式 `X-Conversation-Id` / `X-Rotor-Session-Id`，也可以来自 Rotor
已识别的 Codex、Claude Code 和 OpenCode 请求头。不同 Rotor Token 的 Session 会隔离
命名空间。没有稳定 Session 信号时不启用亲和。

除 `weighted` 外，新 Session 的亲和排序仍然服从优先级边界。adaptive 使用亲和结果
作为同分渠道的决胜顺序。

## Session Lease

`session_lease_enabled=true` 时，Rotor 会持久化
`(Rotor Token, session_id, logical_model) -> channel`。路由前读取仍有效的租约；租约
渠道仍健康且兼容时优先使用。只有成功的上游请求才 assigned/renewed 租约；fallback
成功后迁移到实际成功渠道，因此旧渠道 cooldown 结束也不会立即抢回活跃 Session。

默认 `session_lease_idle_ttl_seconds=1800`，范围为 60–86400 秒。Channel `extra` 可用
`session_lease_idle_ttl_seconds` 覆盖渠道值，或用
`session_lease_idle_ttl_by_model` 对 logical model 和 `*` 配置。它是路由空闲 TTL，
不是 provider cache TTL。

关闭 `affinity_enabled` 会同时停用 Session Lease。没有稳定 Session 信号的请求不会
创建租约。原生 Responses state chain 始终遵循 response ownership，即使租约过期也
不会跨渠道。

Channel 可显式声明 cache、capacity 和 billing scope；当前它们会进入路由与用量
快照，但不会自动合并 cooldown、余额或缓存状态。相关配置见
[Channel 字段](../reference/channel-schema.md)。

## 阶段 4 的数据门槛

成本与 cache-aware 路由仍处于关闭状态。可先用 Control API 或 MCP 按 logical model
检查最近 24 小时的 Session Lease 事实：

```bash
curl -H "Authorization: Bearer $ROTOR_CONTROL_API_TOKEN" \
  "http://127.0.0.1:8000/api/control/v1/usage/session-leases?model=gpt-5.4"
```

`facts_complete_for_evaluation=true` 只表示 Session、Lease、provider usage、usage v2、
费用和币种等事实足以比较，不代表值得启用新策略。Rotor 不设置一个脱离实际流量的固定
样本量，也不会生成综合分数；应跨多个窗口比较同 Channel、费率时段和币种 cohort。

后续若做 shadow 实验，价格首先只影响新 Session 和已过期租约。活跃租约不会因为
峰谷价变化而逐请求迁移；hard failure 的 fallback 仍优先于成本优化。只有受控对照证明
总成本下降且迁移、缓存重建、延迟和失败没有恶化，才讨论显式启用；否则继续保持简单
Session Lease。

策略语义和失败冷却机制见[路由与故障转移](../concepts/routing-and-fallback.md)。
