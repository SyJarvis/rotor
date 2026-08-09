from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from mindagent.core import ActionRisk, AgentContext


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )
    risk: ActionRisk = ActionRisk.READ_ONLY
    require_human_approval: bool = False

    def to_provider_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    agent_context: AgentContext

    @property
    def run_id(self) -> str:
        return self.agent_context.run_id

    @property
    def session_id(self) -> str | None:
        return self.agent_context.session_id

    @property
    def environment(self):
        return self.agent_context.session_environment

    @property
    def policy(self):
        return self.agent_context.session_policy


@dataclass
class ToolProgress:
    message: str = ""
    data: Any = None
    progress: float | None = None

    def __post_init__(self) -> None:
        if self.progress is not None and not 0 <= self.progress <= 1:
            raise ValueError("progress 必须在 0 到 1 之间")


class BaseTool(ABC):
    definition: ToolDefinition

    def assess_risk(
        self,
        arguments: dict[str, Any],
        context: AgentContext | None = None,
    ) -> ActionRisk:
        return self.definition.risk

    @abstractmethod
    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> Any:
        raise NotImplementedError


class StreamingTool(BaseTool):
    @abstractmethod
    async def stream(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> AsyncIterator[ToolProgress]:
        raise NotImplementedError

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> Any:
        last_data: Any = None
        async for progress in self.stream(arguments, context):
            if progress.data is not None:
                last_data = progress.data
        return last_data
