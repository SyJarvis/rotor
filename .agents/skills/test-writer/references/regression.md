# 回归测试

回归测试确保已修复的 bug 不会重现。

## 核心原则

每个回归测试必须：
1. **最小复现** — 用最少的代码还原 bug 触发条件
2. **精确断言** — 断言的恰好是被修复的行为
3. **独立于实现** — 不依赖修复后的内部实现细节

## 回归测试模板

```python
class RegressionTests(unittest.TestCase):
    def test_<issue_id>_<short_description>(self):
        # Bug: <简要描述原始 bug>
        # Fix: <简要描述修复方式>
        # Reproduce: <最小复现步骤>

        # Arrange — 构造触发 bug 的输入
        ...

        # Act — 执行会触发 bug 的操作
        ...

        # Assert — 断言 bug 不再发生
        ...
```

## 示例

### 参数解析 bug

```python
class RegressionTests(unittest.TestCase):
    def test_42_concurrency_key_not_lost_in_serialization(self):
        # Bug: ActionRequest 的 concurrency_key 在 JSON 序列化/反序列化后丢失
        # Fix: 在 from_dict 中正确还原 concurrency_key 字段
        original = ActionRequest(
            action_type=ActionType.TOOL,
            name="write-file",
            arguments={"path": "/tmp/x"},
            concurrency_key="file:/tmp/x",
        )
        serialized = original.to_dict()
        restored = ActionRequest.from_dict(serialized)
        self.assertEqual(restored.concurrency_key, "file:/tmp/x")
```

### 状态机非法转移 bug

```python
class RegressionTests(unittest.TestCase):
    def test_37_policy_rejection_allows_recovery(self):
        # Bug: action 被策略拒绝后，runtime 直接进入 ERROR 状态而非恢复路径
        # Fix: STATE_REJECTION 状态允许转移到 OBSERVING
        machine = AgentStateMachine()
        self.assertTrue(
            machine.can_transition(
                AgentState.ACTION_VALIDATING,
                AgentState.OBSERVING,
            )
        )
```

### 异步竞态条件 bug

```python
class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_58_cancel_protocol_emits_event(self):
        # Bug: 取消运行时没有发出 CANCELLED 事件
        # Fix: Runtime.cancel() 在状态转移时发出事件
        reasoner = BlockingReasoner()
        runtime = AgentRuntime(reasoner=reasoner, executor=EchoExecutor())
        events = []
        runtime.on_event(lambda e: events.append(e))
        task = asyncio.create_task(runtime.run("go"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(any(e.type == EventType.RUN_CANCELLED for e in events))
```

## 快照测试

当输出结构复杂且需要精确匹配时，使用快照测试：

```python
class SnapshotTests(unittest.TestCase):
    def test_conversation_store_output_format(self):
        store = LocalConversationStore(self.tmpdir)
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        store.save("snap-test", messages)
        loaded = store.load("snap-test")
        # 逐字段断言，而非全量比较
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["role"], "user")
        self.assertEqual(loaded[1]["role"], "assistant")
        self.assertEqual(loaded[0]["content"], "hello")
        self.assertEqual(loaded[1]["content"], "world")
```

## 回归测试 Checklist

- [ ] 测试名包含 issue/ticket 编号
- [ ] 注释说明 bug 描述和修复方式
- [ ] 用最小输入触发原始 bug 条件
- [ ] 断言恰好是 bug 修复后的行为
- [ ] 不依赖修复后的内部实现