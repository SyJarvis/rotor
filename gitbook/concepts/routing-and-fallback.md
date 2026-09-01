# 路由与故障转移

路由的输出不是单个渠道，而是一次请求可尝试的有序候选集。

## 候选筛选

一个渠道进入候选集需要满足：

1. 渠道已启用；
2. 请求模型存在于 `models` 或 `model_mapping`；
3. Rotor Token 的 `allowed_channels` 允许该渠道；
4. 渠道满足请求所需能力；
5. 渠道不在临时冷却期，除非所有兼容渠道都在冷却。

最后一条保证单一兼容渠道不会仅因冷却状态而让模型完全不可路由。

## 排序

默认 `priority_weighted` 先保持管理优先级，再按权重形成候选顺序。
`fallback_order` 提供确定性的优先级/ID 顺序；`weighted` 允许权重跨越优先级；
`adaptive` 则只在同一优先级层内根据在线统计评分。

adaptive 的在线成功率、延迟和 in-flight 负载保存在当前进程内，重启后重置。
用于后续分析的候选集、特征和评分快照会写入数据库。

携带稳定 Session ID 的请求还会读取数据库 Session Lease。租约渠道通过上述硬约束且
未冷却时，会移动到候选集首位；因此新 Session 的策略负责初始分配，活跃 Session
默认保持最后成功渠道。租约状态是多 worker 共享的，不随进程重启丢失。

## 何时切换渠道

Rotor 对网络请求错误以及以下上游 HTTP 状态执行 fallback：

```text
401 402 403 404 408 409 425 429 5xx
```

普通 4xx 参数或协议错误不会自动换渠道，以免重复发送一个确定无效的请求。
429 的 `Retry-After` 可用于设置冷却时间。

失败 attempt 不修改 Session Lease。fallback 到其他 Channel 并成功后才迁移租约，
下一 turn 会继续使用新 Channel；旧 Channel 恢复不会立即抢回。租约空闲过期后，
请求重新按新 Session 规则选择。

一旦流式响应已经向客户端发送字节，就无法透明地重放整个响应到另一个渠道。
详见[流式响应与故障转移](../troubleshooting/streaming-and-fallback.md)。
