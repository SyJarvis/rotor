from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from .context import (
    ActionBatch,
    ActionRequest,
    ActionRisk,
    AgentContext,
    AgentError,
    ErrorCode,
    ErrorPhase,
    Observation,
    ObservationBatch,
    ExecutionMode,
)
from .contracts import ActionExecutor


ActionStartedHandler = Callable[[ActionRequest], Awaitable[None]]
ActionFinishedHandler = Callable[
    [ActionRequest, Observation],
    Awaitable[None],
]
ActionCancelledHandler = Callable[
    [ActionRequest, Observation],
    Awaitable[None],
]


class ActionScheduler:
    def __init__(
        self,
        executor: ActionExecutor,
        *,
        allow_parallel_actions: bool = True,
        max_parallel_actions: int = 4,
        action_timeout_s: float | None = 60.0,
        batch_timeout_s: float | None = 120.0,
    ):
        if max_parallel_actions < 1:
            raise ValueError("max_parallel_actions 必须大于 0")
        if action_timeout_s is not None and action_timeout_s <= 0:
            raise ValueError("action_timeout_s 必须大于 0")
        if batch_timeout_s is not None and batch_timeout_s <= 0:
            raise ValueError("batch_timeout_s 必须大于 0")
        self.executor = executor
        self.allow_parallel_actions = allow_parallel_actions
        self.max_parallel_actions = max_parallel_actions
        self.action_timeout_s = action_timeout_s
        self.batch_timeout_s = batch_timeout_s

    async def execute(
        self,
        context: AgentContext,
        batch: ActionBatch,
        *,
        on_action_started: ActionStartedHandler | None = None,
        on_action_finished: ActionFinishedHandler | None = None,
        on_action_cancelled: ActionCancelledHandler | None = None,
    ) -> ObservationBatch:
        started_at = time.time()
        deadline = (
            time.monotonic() + self.batch_timeout_s
            if self.batch_timeout_s is not None
            else None
        )
        observations_by_id: dict[str, Observation] = {}
        semaphore = asyncio.Semaphore(
            self._max_parallel_actions(context)
        )
        active_tasks: set[asyncio.Task[Observation]] = set()

        groups = self._execution_groups(batch)
        try:
            for group_index, group in enumerate(groups):
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    self._add_batch_timeout_observations(
                        observations_by_id,
                        [
                            action
                            for pending_group in groups[group_index:]
                            for action in pending_group
                        ],
                        batch.batch_id,
                    )
                    break

                tasks = {
                    asyncio.create_task(
                        self._execute_action(
                            context,
                            action,
                            semaphore=semaphore,
                            on_action_started=on_action_started,
                            on_action_finished=on_action_finished,
                            on_action_cancelled=on_action_cancelled,
                        )
                    ): action
                    for action in group
                }
                active_tasks.update(tasks)
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=remaining,
                )
                for task in done:
                    active_tasks.discard(task)
                    observation = task.result()
                    observations_by_id[
                        observation.action.action_id
                    ] = observation
                if pending:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    active_tasks.difference_update(pending)
                    self._add_batch_timeout_observations(
                        observations_by_id,
                        [tasks[task] for task in pending],
                        batch.batch_id,
                    )
                    self._add_batch_timeout_observations(
                        observations_by_id,
                        [
                            action
                            for pending_group in groups[group_index + 1 :]
                            for action in pending_group
                        ],
                        batch.batch_id,
                    )
                    break
        except asyncio.CancelledError:
            for task in active_tasks:
                task.cancel()
            await asyncio.gather(
                *active_tasks,
                return_exceptions=True,
            )
            raise

        return ObservationBatch(
            batch_id=batch.batch_id,
            observations=[
                observations_by_id[action.action_id]
                for action in batch.actions
            ],
            started_at=started_at,
            completed_at=time.time(),
        )

    async def _execute_action(
        self,
        context: AgentContext,
        action: ActionRequest,
        *,
        semaphore: asyncio.Semaphore,
        on_action_started: ActionStartedHandler | None,
        on_action_finished: ActionFinishedHandler | None,
        on_action_cancelled: ActionCancelledHandler | None,
    ) -> Observation:
        started: float | None = None
        try:
            async with semaphore:
                if on_action_started is not None:
                    await on_action_started(action)
                started = time.monotonic()
                observation = await asyncio.wait_for(
                    self.executor.execute(context, action),
                    timeout=self.action_timeout_s,
                )
                if not isinstance(observation, Observation):
                    raise TypeError(
                        "ActionExecutor.execute 必须返回 Observation"
                    )
                if observation.action != action:
                    raise ValueError(
                        "Observation.action 与当前 action 不一致"
                    )
        except asyncio.TimeoutError:
            error = AgentError(
                code=ErrorCode.ACTION_TIMEOUT,
                message=f"Action 超过超时时间: {action.name}",
                phase=ErrorPhase.ACTION_EXECUTION,
                recoverable=True,
                details={
                    "action_id": action.action_id,
                    "action_name": action.name,
                    "timeout_s": self.action_timeout_s,
                },
            )
            observation = Observation(
                action=action,
                ok=False,
                error=error.message,
                error_info=error,
            )
        except asyncio.CancelledError:
            observation = self._cancelled_observation(action)
            observation.elapsed_ms = (
                (time.monotonic() - started) * 1000
                if started is not None
                else 0.0
            )
            if on_action_cancelled is not None:
                await on_action_cancelled(action, observation)
            raise
        except Exception as exc:
            error = AgentError(
                code=ErrorCode.ACTION_EXECUTION_FAILED,
                message=str(exc),
                phase=ErrorPhase.ACTION_EXECUTION,
                recoverable=True,
                details={
                    "action_id": action.action_id,
                    "action_name": action.name,
                    "exception_type": type(exc).__name__,
                },
            )
            observation = Observation(
                action=action,
                ok=False,
                error=error.message,
                error_info=error,
            )

        observation.elapsed_ms = (
            (time.monotonic() - started) * 1000
            if started is not None
            else 0.0
        )
        if on_action_finished is not None:
            await on_action_finished(action, observation)
        return observation

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    @staticmethod
    def _cancelled_observation(action: ActionRequest) -> Observation:
        error = AgentError(
            code=ErrorCode.ACTION_CANCELLED,
            message=f"Action 已取消: {action.name}",
            phase=ErrorPhase.ACTION_EXECUTION,
            recoverable=True,
            details={
                "action_id": action.action_id,
                "action_name": action.name,
            },
        )
        return Observation(
            action=action,
            ok=False,
            error=error.message,
            error_info=error,
        )

    @staticmethod
    def _add_batch_timeout_observations(
        observations: dict[str, Observation],
        actions: list[ActionRequest],
        batch_id: str,
    ) -> None:
        for action in actions:
            error = AgentError(
                code=ErrorCode.BATCH_TIMEOUT,
                message=f"ActionBatch 超时: {action.name}",
                phase=ErrorPhase.ACTION_EXECUTION,
                recoverable=True,
                details={
                    "batch_id": batch_id,
                    "action_id": action.action_id,
                    "action_name": action.name,
                },
            )
            observations[action.action_id] = Observation(
                action=action,
                ok=False,
                error=error.message,
                error_info=error,
            )

    def _max_parallel_actions(self, context: AgentContext) -> int:
        session_policy = context.session_policy
        if session_policy is not None:
            return min(
                self.max_parallel_actions,
                session_policy.max_parallel_actions,
            )
        return self.max_parallel_actions

    def _execution_groups(
        self,
        batch: ActionBatch,
    ) -> list[list[ActionRequest]]:
        if (
            not self.allow_parallel_actions
            or batch.execution_mode == ExecutionMode.SEQUENTIAL
        ):
            return [[action] for action in batch.actions]

        groups: list[list[ActionRequest]] = []
        current: list[ActionRequest] = []
        current_keys: set[str] = set()

        for action in batch.actions:
            if action.risk != ActionRisk.READ_ONLY:
                if current:
                    groups.append(current)
                    current = []
                    current_keys = set()
                groups.append([action])
                continue

            key = action.concurrency_key
            if key is not None and key in current_keys:
                groups.append(current)
                current = []
                current_keys = set()

            current.append(action)
            if key is not None:
                current_keys.add(key)

        if current:
            groups.append(current)
        return groups
