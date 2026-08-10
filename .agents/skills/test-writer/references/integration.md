# 集成测试

集成测试验证模块间交互是否正确，重点在接口契约和数据流。

## 契约测试

验证调用方与被调用方之间的协议一致。

### 接口契约

```python
class ProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_reasoner_maps_ask_user_control_call(self):
        # 验证 ProviderReasoner 正确映射 ASK_USER 工具调用
        provider = FakeProvider([
            ProviderResponse(
                tool_calls=[
                    ProviderToolCall(
                        id="ask-1",
                        name=ProviderReasoner.ASK_USER_TOOL,
                        arguments={"message": "Continue?"},
                    )
                ],
            )
        ])
        reasoner = ProviderReasoner(ProviderRouter([provider]), tools=[])
        decision = await reasoner.decide(AgentContext(user_input="go"))
        self.assertEqual(decision.decision_kind, DecisionKind.ASK_USER)
```

### 控制指令冲突检测

```python
def test_control_tool_names_are_reserved(self):
    # 验证用户工具不能覆盖内部控制工具名
    colliding_tool = {
        "type": "function",
        "function": {
            "name": ProviderReasoner.COMPLETE_TOOL,
            "parameters": {"type": "object"},
        },
    }
    with self.assertRaisesRegex(ValueError, "控制决策冲突"):
        ProviderReasoner(ProviderRouter([FakeProvider([])]), tools=[colliding_tool])
```

## 数据流测试

验证数据在模块间传递过程中不被意外修改或丢失。

```python
class ConversationIntegrationTests(unittest.TestCase):
    def test_observation_flows_through_runtime(self):
        # 验证 Observation 从 Executor 经过 Runtime 到达 Reasoner
        reasoner = SequenceReasoner([
            AgentDecision(decision_kind=DecisionKind.ACTIONS, ...),
            AgentDecision(decision_kind=DecisionKind.COMPLETE, ...),
        ])
        executor = EchoExecutor()
        runtime = AgentRuntime(reasoner=reasoner, executor=executor)
        result = runtime.run("test input")
        # 验证 executor 的输出出现在 reasoner 的输入中
        self.assertEqual(len(reasoner.calls), 2)
```

## 组件组合测试

验证不同组件按预期方式组合工作。

### 关键点

- 用 Fake 替换真实依赖，保持测试隔离
- 仅验证组合逻辑，不验证各组件内部行为（那是单元测试的职责）
- 测试组件间的「接缝」——接口调用的参数和返回值

```python
class RuntimeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_preserves_conversation_across_runs(self):
        # Session + ConversationStore + Runtime 组合
        store = LocalConversationStore(self.tmpdir)
        reasoner = RecordingReasoner()
        runtime = AgentRuntime(reasoner=reasoner, executor=EchoExecutor())

        async with Session(runtime=runtime, store=store, ...) as session:
            await session.run("first")
            await session.run("second")

        # 验证第二轮 reasoner 能看到第一轮的上下文
        self.assertEqual(len(reasoner.calls), 2)
        self.assertIn("first", reasoner.calls[1]["messages"])
```

## 调度与并发测试

验证调度策略在并发场景下正确性。

```python
class ActionSchedulerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_actions_run_in_parallel(self):
        executor = TrackingExecutor(delay=0.05)
        scheduler = ActionScheduler(executor=executor)
        actions = [
            ActionRequest(action_type=ActionType.TOOL, name=f"read-{i}",
                          risk=ActionRisk.READ_ONLY)
            for i in range(5)
        ]
        batch = ActionBatch(actions=actions)
        await scheduler.execute(AgentContext(...), batch)
        self.assertEqual(executor.max_active, 5)  # 全部并行

    async def test_destructive_actions_run_sequentially(self):
        executor = TrackingExecutor(delay=0.05)
        scheduler = ActionScheduler(executor=executor)
        actions = [
            ActionRequest(action_type=ActionType.TOOL, name=f"write-{i}",
                          risk=ActionRisk.DESTRUCTIVE)
            for i in range(3)
        ]
        batch = ActionBatch(actions=actions)
        await scheduler.execute(AgentContext(...), batch)
        self.assertEqual(executor.max_active, 1)  # 逐个执行
```

## 集成测试 Checklist

- [ ] 接口契约：调用方传参符合被调用方签名
- [ ] 数据流：数据从输入到输出不被意外修改
- [ ] 错误传播：下游异常能被上游正确捕获和处理
- [ ] 并发安全：共享状态在并发访问下不损坏
- [ ] 资源清理：异步资源（连接、文件句柄）能正确释放