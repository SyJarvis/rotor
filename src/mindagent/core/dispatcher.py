from __future__ import annotations

import asyncio
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
from .contracts import ActionExecutor


class ActionDispatcher:
    def __init__(self) -> None:
        self._executors: dict[ActionType, ActionExecutor] = {}
        self._progress_handlers: list[Any] = []
        self._closed = False

    def register(
        self,
        action_type: ActionType,
        executor: ActionExecutor,
    ) -> None:
        if action_type in self._executors:
            raise ValueError(
                f"ActionType 已注册 executor: {action_type.value}"
            )
        self._executors[action_type] = executor
        add_progress_handler = getattr(
            executor,
            "add_progress_handler",
            None,
        )
        if callable(add_progress_handler):
            for handler in self._progress_handlers:
                add_progress_handler(handler)

    def add_progress_handler(self, handler: Any) -> None:
        if handler in self._progress_handlers:
            return
        self._progress_handlers.append(handler)
        for executor in self._executors.values():
            add_progress_handler = getattr(
                executor,
                "add_progress_handler",
                None,
            )
            if callable(add_progress_handler):
                add_progress_handler(handler)

    async def execute(
        self,
        context: AgentContext,
        action: ActionRequest,
    ) -> Observation:
        return await self.dispatch(context, action)

    async def dispatch(
        self,
        context: AgentContext,
        action: ActionRequest,
    ) -> Observation:
        executor = self._executors.get(action.action_type)
        if executor is None:
            error = AgentError(
                code=ErrorCode.ACTION_TYPE_UNSUPPORTED,
                message=(
                    "未注册 ActionType executor: "
                    f"{action.action_type.value}"
                ),
                phase=ErrorPhase.ACTION_EXECUTION,
                recoverable=True,
                details={
                    "action_id": action.action_id,
                    "action_name": action.name,
                    "action_type": action.action_type.value,
                },
            )
            return Observation(
                action=action,
                ok=False,
                error=error.message,
                error_info=error,
            )
        return await executor.execute(context, action)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for executor in self._executors.values():
            if id(executor) in seen:
                continue
            seen.add(id(executor))
            close = getattr(executor, "close", None)
            if close is None:
                close = getattr(executor, "aclose", None)
            if not callable(close):
                continue
            result = close()
            if asyncio.iscoroutine(result):
                await result
