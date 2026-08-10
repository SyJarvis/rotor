---
name: test-writer
description: 编写高质量测试代码的专项技能。覆盖单元测试、集成测试、回归测试和异步测试的设计模式与最佳实践。当需要为新功能编写测试、为 bug 修复编写回归测试、为现有模块补充测试覆盖、或审查测试质量时触发此技能。
---

# Test Writer

编写高质量 Python 测试代码的技能，覆盖 unittest 和 asyncio 场景。

## 核心原则

1. **最小完整** — 每个测试只验证一个行为，但必须完整（Arrange-Act-Assert 三段齐全）
2. **Fake 优先** — 用轻量 Fake 替代 mock，保持测试可读性
3. **精确定位失败** — 测试失败时应立即看出哪个断言失败，而非需要调试
4. **无外部依赖** — 测试必须可离线、无网络、无真实服务运行

## 工作流

### 1. 识别测试目标

分析被测代码，确定测试层级：

- **纯函数/方法** → 单元测试
- **类间交互/协议契约** → 集成测试
- **已修复的 bug** → 回归测试
- **异步流程** → 异步测试

### 2. 设计测试用例

对每个被测单元，按以下维度覆盖：

| 维度 | 方法 |
|------|------|
| 正常路径 | 等价类划分，每种输入类型一个用例 |
| 边界条件 | 空值、零值、最大值、最小值、空集合 |
| 异常路径 | 非法输入、依赖失败、超时 |
| 状态转移 | 从每个合法状态转移，以及非法转移被拒绝 |

### 3. 编写测试

#### 命名规范

```
class <UnderTest>Tests(unittest.TestCase):
    def test_<what>_<condition>_<expected>(self):
```

示例：`test_transition_from_invalid_state_raises_error`

#### 结构：AAA

```python
def test_save_creates_file(self):
    # Arrange
    store = LocalConversationStore(self.tmpdir)
    messages = [{"role": "user", "content": "hi"}]

    # Act
    store.save("test", messages)

    # Assert
    self.assertEqual(store.load("test"), messages)
```

#### 异步测试

```python
class <UnderTest>Tests(unittest.IsolatedAsyncioTestCase):
    async def test_<what>_<condition>_<expected>(self):
```

### 4. 编写 Fake

当被测代码依赖外部协议时，创建 Fake：

```python
class RecordingReasoner:
    def __init__(self):
        self.calls = []

    async def decide(self, context):
        self.calls.append(context.user_input)
        return AgentDecision(decision_kind=DecisionKind.COMPLETE, ...)
```

Fake 规则：
- 实现被依赖的协议接口（方法名和签名匹配）
- 记录调用参数到 `self.calls` / `self.requests`
- 返回最小可用的合法响应
- 不引入真实依赖（无网络调用、无文件 I/O，除非测试的就是 I/O）

### 5. 验证

写完测试后确认：
- 独立运行通过：`python -m pytest tests/test_xxx.py -v`
- 断言数量恰好覆盖预期行为（不多不少）
- 测试名可直接读出失败原因

## 测试类型详解

- **单元测试 checklist 与模式** → 见 [references/unit-testing.md](references/unit-testing.md)
- **集成测试模式** → 见 [references/integration.md](references/integration.md)
- **回归测试模式** → 见 [references/regression.md](references/regression.md)
- **Python 异步测试模式** → 见 [references/python-async.md](references/python-async.md)

## 反模式（避免）

- ❌ `self.assertTrue(len(result) > 0)` → ✅ `self.assertGreater(len(result), 0)`
- ❌ 在 `setUp` 中做复杂初始化只被部分测试用到 → ✅ 每个测试自包含或在 helper 中按需构造
- ❌ 用 `Mock` 模拟整个类再逐一配置 → ✅ 用轻量 Fake 类
- ❌ 测试之间共享可变状态 → ✅ 每个测试创建新实例
- ❌ 一个测试验证多个不相关行为 → ✅ 拆分为独立测试
- ❌ 对返回值做全文 JSON 比对来断言其中一个字段 → ✅ 只断言关心的字段

## 资源

### references/
- `unit-testing.md` — 单元测试设计 checklist、等价类/边界值方法
- `integration.md` — 集成测试模式：协议契约、模块交互、数据流
- `regression.md` — 回归测试模式：bug 复现、最小用例、快照测试
- `python-async.md` — 异步测试模式：IsolatedAsyncioTestCase、异步 Fake、超时与取消