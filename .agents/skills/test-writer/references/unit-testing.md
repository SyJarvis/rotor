# 单元测试

## Checklist

对每个被测函数/方法/类，逐项确认：

- [ ] 正常路径：每种合法输入类型一个用例
- [ ] 空输入：空字符串、空列表、None、0、空字典
- [ ] 边界值：最大值、最小值、刚好越界
- [ ] 异常路径：非法参数、类型错误、缺少必需参数
- [ ] 副作用：状态变更是否如预期

## 等价类划分

将输入域划分为等价类，每类取一个代表值：

```python
# 被测函数
def classify_age(age):
    if age < 0:
        raise ValueError
    if age < 18:
        return "minor"
    if age < 65:
        return "adult"
    return "senior"

# 等价类：负数 / 0-17 / 18-64 / 65+
# 边界值：-1, 0, 17, 18, 64, 65
class ClassifyAgeTests(unittest.TestCase):
    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            classify_age(-1)

    def test_minor(self):
        self.assertEqual(classify_age(0), "minor")
        self.assertEqual(classify_age(17), "minor")

    def test_adult(self):
        self.assertEqual(classify_age(18), "adult")
        self.assertEqual(classify_age(64), "adult")

    def test_senior(self):
        self.assertEqual(classify_age(65), "senior")
```

## 状态机测试

从状态机定义自动生成测试：

```python
class AgentStateMachineTests(unittest.TestCase):
    def test_declared_transitions_are_accepted(self):
        machine = AgentStateMachine()
        for old_state, new_states in machine.allowed_transitions.items():
            for new_state in new_states:
                context = AgentContext(user_input="test", state=old_state)
                machine.transition(context, new_state)
                self.assertEqual(context.state, new_state)

    def test_undeclared_transitions_are_rejected(self):
        machine = AgentStateMachine()
        for old_state in AgentState:
            for new_state in AgentState:
                if machine.can_transition(old_state, new_state):
                    continue
                context = AgentContext(user_input="test", state=old_state)
                with self.assertRaises(StateMachineError):
                    machine.transition(context, new_state)
```

关键思路：遍历所有状态对，断言合法转移成功、非法转移抛异常。

## 数据类/模型测试

对 dataclass / Pydantic / NamedTuple：

- 构造：合法字段值能正常创建
- 必填字段缺失：`TypeError` 或验证错误
- 不变式：字段间的约束（如 `end_time > start_time`）
- 默认值：未指定可选字段时的行为

```python
class ActionRequestTests(unittest.TestCase):
    def test_required_fields(self):
        with self.assertRaises(TypeError):
            ActionRequest()

    def test_default_optional_fields(self):
        action = ActionRequest(
            action_type=ActionType.TOOL,
            name="read",
            arguments={},
        )
        self.assertIsNone(action.risk)
        self.assertIsNone(action.concurrency_key)
```

## Fake 模式

### Recording Fake（记录调用）

```python
class RecordingExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, context, action):
        self.calls.append(action.action_id)
        return Observation(action=action, ok=True, result=action.name)
```

### Sequence Fake（返回预设序列）

```python
class SequenceReasoner:
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    async def decide(self, context):
        return next(self.decisions)
```

### Conditional Fake（按条件返回）

```python
class SemanticFailureExecutor:
    async def execute(self, context, action):
        return Observation(
            action=action,
            ok=True,
            result={"exit_code": 1, "timed_out": False},
        )
```

## 断言选择指南

| 场景 | 推荐 |
|------|------|
| 相等性 | `assertEqual` / `assertNotEqual` |
| 包含关系 | `assertIn` / `assertNotIn` |
| 布尔值 | `assertTrue` / `assertFalse` |
| 异常 | `assertRaises` / `assertRaisesRegex` |
| 近似值 | `assertAlmostEqual` |
| 类型检查 | `assertIsInstance` |
| None 判断 | `assertIsNone` / `assertIsNotNone` |
| 集合比较 | `assertCountEqual`（无序） / `assertListEqual`（有序） |

避免使用 `assertTrue(expr)` 做等值比较 — 失败信息不明确。改用 `assertEqual` 或 `assertIn`。