from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CleanupFailure:
    resource: str
    exception_type: str
    error: str


@dataclass(frozen=True)
class CloseReport:
    closed: bool
    timed_out: bool
    elapsed_s: float
    failures: tuple[CleanupFailure, ...] = ()
    remaining_tasks: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.timed_out and not self.failures and not self.remaining_tasks


async def wait_for_cleanup(
    awaitables: dict[str, Any],
    *,
    deadline: float | None,
    failures: list[CleanupFailure],
    remaining_tasks: list[str],
) -> None:
    tasks = {
        name: asyncio.ensure_future(awaitable)
        for name, awaitable in awaitables.items()
    }
    timeout = (
        None
        if deadline is None
        else max(0.0, deadline - time.monotonic())
    )
    done, pending = await asyncio.wait(tasks.values(), timeout=timeout)
    names_by_task = {task: name for name, task in tasks.items()}
    for task in done:
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            failures.append(
                CleanupFailure(
                    resource=names_by_task[task],
                    exception_type=type(exception).__name__,
                    error=str(exception),
                )
            )
            continue
        result = task.result()
        if isinstance(result, CloseReport):
            resource = names_by_task[task]
            failures.extend(
                CleanupFailure(
                    resource=f"{resource}/{failure.resource}",
                    exception_type=failure.exception_type,
                    error=failure.error,
                )
                for failure in result.failures
            )
            remaining_tasks.extend(
                f"{resource}/{remaining}"
                for remaining in result.remaining_tasks
            )
    for task in pending:
        task.cancel()
    remaining_tasks.extend(
        sorted(names_by_task[task] for task in pending)
    )
