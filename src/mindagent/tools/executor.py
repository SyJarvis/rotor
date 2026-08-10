from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mindagent.core import (
    ActionRequest,
    ActionType,
    AgentError,
    AgentContext,
    ErrorCode,
    ErrorPhase,
    Observation,
)

from .base import StreamingTool, ToolContext, ToolProgress
from .registry import ToolRegistry


ToolProgressHandler = Callable[
    [AgentContext, ActionRequest, ToolProgress],
    Awaitable[None],
]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        progress_handler: ToolProgressHandler | None = None,
    ):
        self.registry = registry
        self._progress_handlers: list[ToolProgressHandler] = []
        if progress_handler:
            self._progress_handlers.append(progress_handler)

    def add_progress_handler(self, handler: ToolProgressHandler) -> None:
        if handler not in self._progress_handlers:
            self._progress_handlers.append(handler)

    async def close(self) -> None:
        await self.registry.close()

    async def execute(
        self,
        context: AgentContext,
        action: ActionRequest,
    ) -> Observation:
        if action.action_type != ActionType.TOOL:
            error = AgentError(
                code=ErrorCode.ACTION_EXECUTION_FAILED,
                message=(
                    "ToolExecutor 只处理 tool_call，"
                    f"收到 {action.action_type.value}"
                ),
                phase=ErrorPhase.ACTION_EXECUTION,
                recoverable=True,
                details={"action_name": action.name},
            )
            return Observation(
                action=action,
                ok=False,
                error=error.message,
                error_info=error,
            )

        try:
            tool = self.registry.get(action.name)
            self.registry.validate_arguments(action.name, action.arguments)
            tool_context = ToolContext(agent_context=context)
            if isinstance(tool, StreamingTool):
                result = await self._execute_streaming(
                    context,
                    action,
                    tool,
                    tool_context,
                )
            else:
                result = await tool.execute(action.arguments, tool_context)
            return Observation(action=action, ok=True, result=result)
        except Exception as exc:
            error = AgentError(
                code=ErrorCode.ACTION_EXECUTION_FAILED,
                message=str(exc),
                phase=ErrorPhase.ACTION_EXECUTION,
                recoverable=True,
                details={
                    "action_name": action.name,
                    "exception_type": type(exc).__name__,
                },
            )
            return Observation(
                action=action,
                ok=False,
                error=error.message,
                error_info=error,
            )

    async def _execute_streaming(
        self,
        context: AgentContext,
        action: ActionRequest,
        tool: StreamingTool,
        tool_context: ToolContext,
    ) -> Any:
        last_data: Any = None
        async for progress in tool.stream(action.arguments, tool_context):
            if not isinstance(progress, ToolProgress):
                raise TypeError("StreamingTool.stream 必须产生 ToolProgress")
            if progress.data is not None:
                last_data = progress.data
            for handler in self._progress_handlers:
                await handler(context, action, progress)
        return last_data
