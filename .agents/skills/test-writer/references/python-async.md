# Python 异步测试

## 测试基类

```python
import unittest

class SyncTests(unittest.TestCase):
    """同步测试用此基类"""

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    """异步测试用此基类，每个测试方法运行独立事件循环"""
```

关键区别：
- `unittest.TestCase` — 同步，`setUp/tearDown`
- `unittest.IsolatedAsyncioTestCase` — 异步，`asyncSetUp/asyncTearDown`

## 异步 Fake

### 同步方法 Fake（用于非协议依赖）

```python
class FakeProvider(BaseProvider):
    name = "fake"
    capabilities = ProviderCapabilities(tool_calling=True)

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def chat(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        return next(self.responses)
```

### 异步迭代 Fake（用于流式接口）

```python
class FakeStreamingProvider(BaseProvider):
    name = "streaming"
    capabilities = ProviderCapabilities(tool_calling=True, streaming=True)

    def __init__(self, chunks):
        self.chunks = chunks

    async def chat(self, messages, **kwargs):
        raise AssertionError("不应调用非流式接口")

    async def stream_chat(self, messages, **kwargs):
        for chunk in self.chunks:
            yield chunk
```

### 阻塞 Fake（用于超时/取消测试）

```python
class BlockingReasoner:
    def __init__(self):
        self.started = asyncio.Event()

    async def decide(self, context):
        self.started.set()
        await asyncio.sleep(60)  # 永不返回，用于模拟长时间运行
```

## 常见异步测试模式

### 超时测试

```python
async def test_runtime_timeout(self):
    reasoner = BlockingReasoner()
    executor = EchoExecutor()
    config = RunConfig(timeout_seconds=0.5)
    runtime = AgentRuntime(reasoner=reasoner, executor=executor)
    result = await runtime.run("go", config=config)
    self.assertEqual(result.outcome, RunOutcome.TIMEOUT)
```

### 取消测试

```python
async def test_session_cancel(self):
    reasoner = BlockingReasoner()
    runtime = AgentRuntime(reasoner=reasoner, executor=EchoExecutor())
    task = asyncio.create_task(runtime.run("go"))
    await reasoner.started.wait()  # 确保 reasoner 已启动
    task.cancel()
    with self.assertRaises(asyncio.CancelledError):
        await task
```

### 并发测试

```python
async def test_concurrent_actions_respect_concurrency_key(self):
    executor = TrackingExecutor(delay=0.05)
    scheduler = ActionScheduler(executor=executor)
    actions = [
        ActionRequest(
            action_type=ActionType.TOOL,
            name="write",
            arguments={"key": f"k{i}"},
            concurrency_key="shared-resource",
        )
        for i in range(3)
    ]
    batch = ActionBatch(actions=actions)
    await scheduler.execute(AgentContext(...), batch)
    # 同一 concurrency_key 的操作不应并行
    self.assertEqual(len(executor.key_conflicts), 0)
```

## 异步测试 Checklist

- [ ] 异步测试继承 `unittest.IsolatedAsyncioTestCase`
- [ ] 异步 `setUp` 用 `asyncSetUp` 而非 `setUp`
- [ ] 异步清理用 `asyncTearDown`
- [ ] 不在异步测试中用 `time.sleep` — 用 `asyncio.sleep`
- [ ] 等待条件用 `asyncio.Event` 或 `asyncio.wait_for`，而非空 sleep
- [ ] 取消测试中先等待任务启动再取消
- [ ] 每个异步测试创建新的 Fake 实例，不共享状态