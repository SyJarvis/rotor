from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .context import (
    ActionRequest,
    ActionType,
    AgentContext,
    AgentError,
    ErrorCode,
    ErrorPhase,
    Observation,
)


SubAgentRunner = Callable[
    [AgentContext, str, dict[str, Any]],
    Awaitable[Any],
]


class SubAgentExecutor:
    DELEGATE = "agent_delegate"

    def __init__(
        self,
        runner: SubAgentRunner,
        background_runner: SubAgentRunner | None = None,
    ):
        self.runner = runner
        self.background_runner = background_runner

    @classmethod
    def action_types(cls) -> dict[str, ActionType]:
        return {cls.DELEGATE: ActionType.AGENT}

    @classmethod
    def tool_schemas(cls) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": cls.DELEGATE,
                    "description": (
                        "Delegate a task to an optional sub-agent. By "
                        "default the parent waits for the final result; set "
                        "context.mode to background to return a task ref."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "context": {"type": "object"},
                        },
                        "required": ["task"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    async def execute(
        self,
        context: AgentContext,
        action: ActionRequest,
    ) -> Observation:
        if action.action_type != ActionType.AGENT:
            return self._failure(
                action,
                f"SubAgentExecutor 不处理 {action.action_type.value}",
            )
        task = action.arguments.get("task")
        child_context = action.arguments.get("context", {})
        if not isinstance(task, str) or not task.strip():
            return self._failure(action, "子 Agent task 必须是非空字符串")
        if not isinstance(child_context, dict):
            return self._failure(action, "子 Agent context 必须是 object")
        mode = child_context.get("mode", "await_result")
        if mode not in {"await_result", "background"}:
            return self._failure(
                action,
                "子 Agent context.mode 必须是 await_result 或 background",
            )
        try:
            if mode == "background":
                if self.background_runner is None:
                    return self._failure(
                        action,
                        "当前 SubAgentExecutor 未配置 background runner",
                    )
                result = await self.background_runner(
                    context,
                    task,
                    child_context,
                )
            else:
                result = await self.runner(context, task, child_context)
            return Observation(action=action, ok=True, result=result)
        except Exception as exc:
            return self._failure(action, str(exc), exc)

    @staticmethod
    def _failure(
        action: ActionRequest,
        message: str,
        exc: Exception | None = None,
    ) -> Observation:
        error = AgentError(
            code=ErrorCode.ACTION_EXECUTION_FAILED,
            message=message,
            phase=ErrorPhase.ACTION_EXECUTION,
            recoverable=True,
            details={
                "action_name": action.name,
                "exception_type": type(exc).__name__ if exc else None,
            },
        )
        return Observation(
            action=action,
            ok=False,
            error=message,
            error_info=error,
        )
