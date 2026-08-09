from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OrchestrationMode(str, Enum):
    SINGLE_AGENT = "single_agent"
    MULTI_AGENT = "multi_agent"
    PIPELINE = "pipeline"


@dataclass(frozen=True)
class PlannedAgentTask:
    agent_id: str
    role: str
    task: str
    boundaries: tuple[str, ...] = ()
    expected_output: str = ""
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("planned agent_id 不能为空")
        if not self.task.strip():
            raise ValueError("planned agent task 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "task": self.task,
            "boundaries": list(self.boundaries),
            "expected_output": self.expected_output,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlannedAgentTask":
        return cls(
            agent_id=_require_str(data, "agent_id"),
            role=str(data.get("role", "")),
            task=_require_str(data, "task"),
            boundaries=tuple(_string_list(data.get("boundaries", []))),
            expected_output=str(data.get("expected_output", "")),
            depends_on=tuple(_string_list(data.get("depends_on", []))),
        )


@dataclass(frozen=True)
class OrchestrationPlan:
    mode: OrchestrationMode
    confidence: float
    reason: str
    parallelizable: bool = False
    agents: tuple[PlannedAgentTask, ...] = ()
    merge_strategy: str = ""
    max_child_agents: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.mode, OrchestrationMode):
            raise ValueError("orchestration mode 必须是 OrchestrationMode")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence 必须在 0 到 1 之间")
        if self.max_child_agents < 0:
            raise ValueError("max_child_agents 不能小于 0")
        if (
            self.mode != OrchestrationMode.SINGLE_AGENT
            and not self.agents
        ):
            raise ValueError("multi_agent/pipeline plan 必须包含 agents")
        if len(self.agents) > self.max_child_agents:
            raise ValueError("agents 数量超过 max_child_agents")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "parallelizable": self.parallelizable,
            "agents": [agent.to_dict() for agent in self.agents],
            "merge_strategy": self.merge_strategy,
            "max_child_agents": self.max_child_agents,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrchestrationPlan":
        return cls(
            mode=OrchestrationMode(_require_str(data, "mode")),
            confidence=float(data.get("confidence", 0)),
            reason=str(data.get("reason", "")),
            parallelizable=bool(data.get("parallelizable", False)),
            agents=tuple(
                PlannedAgentTask.from_dict(item)
                for item in data.get("agents", [])
                if isinstance(item, dict)
            ),
            merge_strategy=str(data.get("merge_strategy", "")),
            max_child_agents=int(data.get("max_child_agents", 3)),
        )


JUDGE_PROMPT_TEMPLATE = """你是 MindAgent 的 Orchestration Judge。你的任务不是解决用户问题，而是判断这个问题是否应该由多个子 Agent 协同完成。

你必须在 single_agent、multi_agent、pipeline 中选择一种模式。

选择 single_agent，当：
- 用户问题简单、单一、低风险
- 任务无法自然拆分为独立子任务
- 多 Agent 会增加不必要成本
- 一个 Agent 串行完成更可靠

选择 multi_agent，当：
- 用户显式要求多个角色、多个角度、并行分析或交叉验证
- 任务需要多个独立专业视角
- 任务需要同时采集多类证据
- 任务是诊断、排查、审查、评估、方案设计、对比、验证
- 子任务之间可以并行执行，最后由主 Agent 汇总

选择 pipeline，当：
- 任务存在明确顺序依赖
- 前一步输出是后一步输入

约束：
- 默认最多建议 3 个子 Agent
- 每个子 Agent 的任务必须有明确边界
- 子 Agent 不应再创建子 Agent
- 子 Agent 只能执行有界任务
- 写操作、危险操作、联网、删除、修改系统配置等必须写入 boundaries
- 如果不确定，选择 single_agent

只输出 JSON，不输出解释性自然语言。

用户问题：
{user_input}

当前可用 Agent：
{available_agents}

输出 JSON schema：
{schema}
"""


PLANNER_MULTI_AGENT_PROMPT_TEMPLATE = """你是 MindAgent 主 Agent。Orchestration Judge 建议使用多个子 Agent 协同完成任务。

你必须遵守以下编排计划：

{plan_json}

执行规则：
1. 如果还没有子 Agent observations，你必须调用 agent_delegate。
2. 对 orchestration_plan.agents 中的每个 agent 创建一个 agent_delegate 调用。
3. 每个 agent_delegate 的 task 必须包含子任务目标、明确边界和 expected_output。
4. 所有无依赖的子 Agent 任务应在同一个 action batch 中发起。
5. 收到子 Agent observations 后，不要再次委派。
6. 你必须综合子 Agent 结果，输出总体结论、每个子 Agent 的关键发现、一致点、冲突或不确定点、下一步建议。
7. 如果某个子 Agent 失败，基于已有结果给出部分结论，并明确失败影响。
8. 不得编造未返回的子 Agent 结果。
"""


def build_orchestration_judge_prompt(
    user_input: str,
    available_agents: list[dict[str, Any]],
) -> str:
    return JUDGE_PROMPT_TEMPLATE.format(
        user_input=user_input,
        available_agents=json.dumps(
            available_agents,
            ensure_ascii=False,
            indent=2,
        ),
        schema=json.dumps(
            _plan_schema(),
            ensure_ascii=False,
            indent=2,
        ),
    )


def build_multi_agent_planner_prompt(plan: OrchestrationPlan) -> str:
    return PLANNER_MULTI_AGENT_PROMPT_TEMPLATE.format(
        plan_json=json.dumps(
            plan.to_dict(),
            ensure_ascii=False,
            indent=2,
        )
    )


def parse_orchestration_plan(text: str) -> OrchestrationPlan:
    return OrchestrationPlan.from_dict(json.loads(text))


def _plan_schema() -> dict[str, Any]:
    return {
        "mode": "single_agent | multi_agent | pipeline",
        "confidence": "number between 0 and 1",
        "reason": "string",
        "parallelizable": "boolean",
        "agents": [
            {
                "agent_id": "string",
                "role": "string",
                "task": "string",
                "boundaries": ["string"],
                "expected_output": "string",
                "depends_on": ["string"],
            }
        ],
        "merge_strategy": "string",
        "max_child_agents": "number",
    }


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("字段必须是字符串列表")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("字段必须是字符串列表")
    return value
