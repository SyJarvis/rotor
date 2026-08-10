from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from .context import AgentContext, AgentResult
from .event import EventDeliveryFailure
from .lifecycle import CleanupFailure, CloseReport, wait_for_cleanup
from .runtime import AgentRuntime
from .session import SessionEnvironment, SessionPolicy


class AgentTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    FINAL = "final"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DelegationMode(str, Enum):
    AWAIT_RESULT = "await_result"
    BACKGROUND = "background"


class AgentTaskEventType(str, Enum):
    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_MESSAGE = "task_message"
    TASK_NEEDS_INPUT = "task_needs_input"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"


@dataclass(frozen=True)
class AgentTaskEvent:
    event_type: AgentTaskEventType
    task_id: str
    agent_id: str
    run_id: str | None = None
    parent_task_id: str | None = None
    parent_run_id: str | None = None
    status: AgentTaskStatus | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    sequence: int = 0
    dropped_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "parent_run_id": self.parent_run_id,
            "status": self.status.value if self.status else None,
            "payload": self.payload,
            "event_id": self.event_id,
            "created_at": self.created_at,
            "sequence": self.sequence,
            "dropped_events": self.dropped_events,
        }


@dataclass(frozen=True)
class AgentWorkerSpec:
    agent_id: str
    runtime: AgentRuntime
    session_environment: SessionEnvironment | None = None
    session_policy: SessionPolicy | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent_id 不能为空")


@dataclass(frozen=True)
class AgentTaskResult:
    task_id: str
    agent_id: str
    run_id: str
    status: AgentTaskStatus
    final_answer: str | None
    error: str | None
    evidence_ids: tuple[str, ...] = ()
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "final_answer": self.final_answer,
            "error": self.error,
            "evidence_ids": list(self.evidence_ids),
            "output": self.output,
        }


@dataclass(frozen=True)
class AgentTaskRef:
    task_id: str
    run_id: str
    agent_id: str
    parent_task_id: str | None = None
    parent_run_id: str | None = None
    depth: int = 0
    status: AgentTaskStatus = AgentTaskStatus.PENDING
    mode: DelegationMode = DelegationMode.BACKGROUND

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "parent_task_id": self.parent_task_id,
            "parent_run_id": self.parent_run_id,
            "depth": self.depth,
            "status": self.status.value,
            "mode": self.mode.value,
        }


@dataclass
class AgentTaskRecord:
    task_id: str
    agent_id: str
    task: str
    parent_task_id: str | None = None
    parent_run_id: str | None = None
    depth: int = 0
    run_id: str | None = None
    status: AgentTaskStatus = AgentTaskStatus.PENDING
    input_context: dict[str, Any] = field(default_factory=dict)
    result: AgentTaskResult | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "task": self.task,
            "parent_task_id": self.parent_task_id,
            "parent_run_id": self.parent_run_id,
            "depth": self.depth,
            "run_id": self.run_id,
            "status": self.status.value,
            "input_context": self.input_context,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AgentTaskView:
    task_id: str
    agent_id: str
    run_id: str | None
    status: AgentTaskStatus
    task: str
    parent_task_id: str | None
    parent_run_id: str | None
    depth: int
    is_background: bool
    recent_events: tuple[AgentTaskEvent, ...]
    result: AgentTaskResult | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "task": self.task,
            "parent_task_id": self.parent_task_id,
            "parent_run_id": self.parent_run_id,
            "depth": self.depth,
            "is_background": self.is_background,
            "recent_events": [
                event.to_dict() for event in self.recent_events
            ],
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class _EventWatcher:
    queue: asyncio.Queue[AgentTaskEvent | None]
    task_id: str | None = None
    parent_task_id: str | None = None

    def matches(self, event: AgentTaskEvent) -> bool:
        if self.task_id is not None and event.task_id != self.task_id:
            return False
        if (
            self.parent_task_id is not None
            and event.parent_task_id != self.parent_task_id
        ):
            return False
        return True


class AgentOrchestrator:
    """In-process registry and execution graph for bounded agent runs."""

    def __init__(
        self,
        event_handler: Callable[[AgentTaskEvent], Awaitable[None]]
        | None = None,
        *,
        max_records: int = 1000,
        max_events: int = 5000,
        event_queue_size: int = 1000,
        max_background_tasks: int = 8,
        max_concurrent_tasks: int = 8,
        max_task_depth: int = 4,
        max_children_per_task: int = 8,
        task_timeout_s: float | None = None,
        event_handler_timeout_s: float | None = 5.0,
    ) -> None:
        if max_records < 1:
            raise ValueError("max_records 必须大于 0")
        if max_events < 1:
            raise ValueError("max_events 必须大于 0")
        if event_queue_size < 1:
            raise ValueError("event_queue_size 必须大于 0")
        if max_background_tasks < 1:
            raise ValueError("max_background_tasks 必须大于 0")
        if max_concurrent_tasks < 1:
            raise ValueError("max_concurrent_tasks 必须大于 0")
        if max_task_depth < 0:
            raise ValueError("max_task_depth 不能小于 0")
        if max_children_per_task < 1:
            raise ValueError("max_children_per_task 必须大于 0")
        if task_timeout_s is not None and task_timeout_s <= 0:
            raise ValueError("task_timeout_s 必须大于 0")
        if event_handler_timeout_s is not None and event_handler_timeout_s <= 0:
            raise ValueError("event_handler_timeout_s 必须大于 0")
        self._workers: dict[str, AgentWorkerSpec] = {}
        self._records: dict[str, AgentTaskRecord] = {}
        self._run_to_task: dict[str, str] = {}
        self._events: list[AgentTaskEvent] = []
        self._background_tasks: dict[str, asyncio.Task[AgentTaskResult]] = {}
        self._foreground_tasks: dict[str, asyncio.Task[Any]] = {}
        self._event_watchers: list[_EventWatcher] = []
        self._event_delivery_failures: deque[EventDeliveryFailure] = deque(
            maxlen=max_events
        )
        self._terminal_event_task_ids: set[str] = set()
        self._next_event_sequence = 1
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._close_report: CloseReport | None = None
        self.event_handler = event_handler
        self.max_records = max_records
        self.max_events = max_events
        self.event_queue_size = event_queue_size
        self.max_background_tasks = max_background_tasks
        self.max_concurrent_tasks = max_concurrent_tasks
        self.max_task_depth = max_task_depth
        self.max_children_per_task = max_children_per_task
        self.task_timeout_s = task_timeout_s
        self.event_handler_timeout_s = event_handler_timeout_s
        self._active_executions = 0

    def register_agent(
        self,
        agent_id: str,
        runtime: AgentRuntime,
        *,
        session_environment: SessionEnvironment | None = None,
        session_policy: SessionPolicy | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._ensure_open()
        if agent_id in self._workers:
            raise ValueError(f"agent 已注册: {agent_id}")
        self._workers[agent_id] = AgentWorkerSpec(
            agent_id=agent_id,
            runtime=runtime,
            session_environment=session_environment,
            session_policy=session_policy,
            metadata=metadata or {},
        )

    def unregister_agent(self, agent_id: str) -> bool:
        if any(
            record.agent_id == agent_id
            and record.status
            in {AgentTaskStatus.PENDING, AgentTaskStatus.RUNNING}
            for record in self._records.values()
        ):
            raise RuntimeError(f"agent 仍有运行中任务: {agent_id}")
        return self._workers.pop(agent_id, None) is not None

    def get_agent(self, agent_id: str) -> AgentWorkerSpec | None:
        return self._workers.get(agent_id)

    def list_agent_ids(self) -> list[str]:
        return list(self._workers.keys())

    async def run_agent(
        self,
        agent_id: str,
        task: str,
        *,
        input_context: dict[str, Any] | None = None,
        parent_context: AgentContext | None = None,
        parent_task_id: str | None = None,
        task_id: str | None = None,
        session_environment: SessionEnvironment | None = None,
        session_policy: SessionPolicy | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentTaskResult:
        self._ensure_open()
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("AgentOrchestrator 必须在 asyncio task 中运行")
        resolved_task_id = task_id or str(uuid.uuid4())
        self._foreground_tasks[resolved_task_id] = current_task
        record: AgentTaskRecord | None = None
        try:
            worker = self._require_worker(agent_id)
            if not task.strip():
                raise ValueError("agent task 不能为空")
            record = await self._start_record(
                agent_id,
                task,
                input_context=input_context,
                parent_context=parent_context,
                parent_task_id=parent_task_id,
                task_id=resolved_task_id,
                metadata=metadata,
            )
            return await self._execute_record(
                worker,
                record,
                parent_context=parent_context,
                session_environment=session_environment,
                session_policy=session_policy,
                metadata=metadata,
                raise_on_cancel=True,
                raise_on_error=True,
            )
        except asyncio.CancelledError:
            cancelled_record = record or self._records.get(resolved_task_id)
            if cancelled_record is not None and cancelled_record.result is None:
                result = self._mark_record_cancelled(cancelled_record)
                await asyncio.shield(
                    self._emit_result(cancelled_record, result)
                )
            raise
        finally:
            self._foreground_tasks.pop(resolved_task_id, None)

    async def start_agent(
        self,
        agent_id: str,
        task: str,
        *,
        input_context: dict[str, Any] | None = None,
        parent_context: AgentContext | None = None,
        parent_task_id: str | None = None,
        task_id: str | None = None,
        session_environment: SessionEnvironment | None = None,
        session_policy: SessionPolicy | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentTaskRef:
        self._ensure_open()
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("AgentOrchestrator 必须在 asyncio task 中运行")
        resolved_task_id = task_id or str(uuid.uuid4())
        self._foreground_tasks[resolved_task_id] = current_task
        record: AgentTaskRecord | None = None
        try:
            worker = self._require_worker(agent_id)
            if not task.strip():
                raise ValueError("agent task 不能为空")
            if len(self._background_tasks) >= self.max_background_tasks:
                raise RuntimeError("后台 Agent 任务数量已达上限")

            record = await self._start_record(
                agent_id,
                task,
                input_context=input_context,
                parent_context=parent_context,
                parent_task_id=parent_task_id,
                task_id=resolved_task_id,
                metadata=metadata,
            )
            background_task = asyncio.create_task(
                self._run_background_record(
                    worker,
                    record,
                    parent_context=parent_context,
                    session_environment=session_environment,
                    session_policy=session_policy,
                    metadata=metadata,
                ),
                name=f"mindagent-background-{record.task_id}",
            )
            self._background_tasks[record.task_id] = background_task
            background_task.add_done_callback(
                lambda task, task_id=record.task_id: self._background_tasks.pop(
                    task_id,
                    None,
                )
            )
            return AgentTaskRef(
                task_id=record.task_id,
                run_id=record.run_id or "",
                agent_id=record.agent_id,
                parent_task_id=record.parent_task_id,
                parent_run_id=record.parent_run_id,
                depth=record.depth,
                status=record.status,
            )
        except asyncio.CancelledError:
            cancelled_record = record or self._records.get(resolved_task_id)
            if cancelled_record is not None and cancelled_record.result is None:
                result = self._mark_record_cancelled(cancelled_record)
                await asyncio.shield(
                    self._emit_result(cancelled_record, result)
                )
            raise
        finally:
            self._foreground_tasks.pop(resolved_task_id, None)

    async def wait_task(self, task_id: str) -> AgentTaskResult:
        background_task = self._background_tasks.get(task_id)
        if background_task is not None:
            return await background_task
        record = self._records.get(task_id)
        if record is None:
            raise KeyError(f"未知 task_id: {task_id}")
        if record.result is None:
            raise RuntimeError(f"task 尚未完成: {task_id}")
        return record.result

    def cancel_task(self, task_id: str) -> bool:
        task_ids = [task_id, *self._descendant_task_ids(task_id)]
        cancelled = False
        current_task = asyncio.current_task()
        for candidate_id in reversed(task_ids):
            task = self._background_tasks.get(candidate_id)
            if task is None:
                task = self._foreground_tasks.get(candidate_id)
            if task is not None and task is not current_task and not task.done():
                task.cancel()
                cancelled = True
        return cancelled

    def list_running_tasks(
        self,
        parent_task_id: str | None = None,
    ) -> list[AgentTaskRecord]:
        records = [
            self._records[task_id]
            for task_id in self._background_tasks
            if task_id in self._records
        ]
        if parent_task_id is None:
            return records
        return [
            record
            for record in records
            if record.parent_task_id == parent_task_id
        ]

    async def watch_events(
        self,
        *,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        replay: bool = True,
    ) -> AsyncIterator[AgentTaskEvent]:
        self._ensure_open()
        watcher = _EventWatcher(
            asyncio.Queue(maxsize=self.event_queue_size),
            task_id=task_id,
            parent_task_id=parent_task_id,
        )
        if replay:
            for event in self._events:
                if watcher.matches(event):
                    yield event
        self._event_watchers.append(watcher)
        try:
            while True:
                event = await watcher.queue.get()
                if event is None:
                    return
                yield event
        finally:
            if watcher in self._event_watchers:
                self._event_watchers.remove(watcher)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def event_delivery_failures(self) -> tuple[EventDeliveryFailure, ...]:
        return tuple(self._event_delivery_failures)

    async def __aenter__(self) -> AgentOrchestrator:
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def close(
        self,
        *,
        close_workers: bool = False,
        timeout_s: float | None = 10.0,
    ) -> CloseReport:
        """Cancel owned tasks and optionally close registered runtimes."""
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("timeout_s 必须大于 0")
        async with self._close_lock:
            if self._closed:
                assert self._close_report is not None
                return self._close_report
            started_at = time.monotonic()
            deadline = (
                None if timeout_s is None else started_at + timeout_s
            )
            failures: list[CleanupFailure] = []
            remaining_tasks: list[str] = []
            self._closing = True
            try:
                current_task = asyncio.current_task()
                tasks = {
                    *self._background_tasks.values(),
                    *self._foreground_tasks.values(),
                }
                tasks.discard(current_task)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await wait_for_cleanup(
                        {
                            task.get_name(): task
                            for task in tasks
                        },
                        deadline=deadline,
                        failures=failures,
                        remaining_tasks=remaining_tasks,
                    )
                self._background_tasks.clear()
                self._foreground_tasks.clear()

                for watcher in list(self._event_watchers):
                    if watcher.queue.full():
                        watcher.queue.get_nowait()
                    watcher.queue.put_nowait(None)
                self._event_watchers.clear()

                if close_workers:
                    runtimes = {
                        id(worker.runtime): worker.runtime
                        for worker in self._workers.values()
                    }
                    await wait_for_cleanup(
                        {
                            f"worker-runtime:{runtime_id}": runtime.close(
                                close_dependencies=True,
                                timeout_s=(
                                    None
                                    if deadline is None
                                    else max(
                                        0.001,
                                        deadline - time.monotonic(),
                                    )
                                ),
                            )
                            for runtime_id, runtime in runtimes.items()
                        },
                        deadline=deadline,
                        failures=failures,
                        remaining_tasks=remaining_tasks,
                    )
            finally:
                self._closed = True
                self._closing = False
                elapsed_s = time.monotonic() - started_at
                self._close_report = CloseReport(
                    closed=True,
                    timed_out=bool(remaining_tasks),
                    elapsed_s=elapsed_s,
                    failures=tuple(failures),
                    remaining_tasks=tuple(remaining_tasks),
                )
            return self._close_report

    async def run_child(
        self,
        parent_context: AgentContext,
        task: str,
        child_context: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = child_context.get("agent_id") or child_context.get("role")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("子 Agent context 需要 agent_id 或 role")
        task_id = child_context.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise ValueError("子 Agent task_id 必须是字符串")
        result = await self.run_agent(
            agent_id,
            task,
            input_context=child_context,
            parent_context=parent_context,
            task_id=task_id,
        )
        return result.to_dict()

    async def start_child(
        self,
        parent_context: AgentContext,
        task: str,
        child_context: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = child_context.get("agent_id") or child_context.get("role")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("子 Agent context 需要 agent_id 或 role")
        task_id = child_context.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise ValueError("子 Agent task_id 必须是字符串")
        task_ref = await self.start_agent(
            agent_id,
            task,
            input_context=child_context,
            parent_context=parent_context,
            task_id=task_id,
        )
        return task_ref.to_dict()

    def get_task(self, task_id: str) -> AgentTaskRecord | None:
        return self._records.get(task_id)

    def get_task_by_run_id(self, run_id: str) -> AgentTaskRecord | None:
        task_id = self._run_to_task.get(run_id)
        if task_id is None:
            return None
        return self._records.get(task_id)

    def children_of(self, task_id: str) -> list[AgentTaskRecord]:
        return [
            record
            for record in self._records.values()
            if record.parent_task_id == task_id
        ]

    def execution_tree(self, root_task_id: str) -> dict[str, Any]:
        record = self._records[root_task_id]
        return {
            **record.to_dict(),
            "children": [
                self.execution_tree(child.task_id)
                for child in self.children_of(root_task_id)
            ],
        }

    def list_tasks(self) -> list[AgentTaskRecord]:
        return list(self._records.values())

    def list_events(self, task_id: str | None = None) -> list[AgentTaskEvent]:
        if task_id is None:
            return list(self._events)
        return [event for event in self._events if event.task_id == task_id]

    def task_view(
        self,
        task_id: str,
        *,
        recent_event_limit: int = 20,
    ) -> AgentTaskView | None:
        record = self._records.get(task_id)
        if record is None:
            return None
        recent_events = tuple(self.list_events(task_id)[-recent_event_limit:])
        return AgentTaskView(
            task_id=record.task_id,
            agent_id=record.agent_id,
            run_id=record.run_id,
            status=record.status,
            task=record.task,
            parent_task_id=record.parent_task_id,
            parent_run_id=record.parent_run_id,
            depth=record.depth,
            is_background=record.task_id in self._background_tasks,
            recent_events=recent_events,
            result=record.result,
            error=record.error,
        )

    def task_tree_view(
        self,
        root_task_id: str,
        *,
        recent_event_limit: int = 20,
    ) -> dict[str, Any]:
        view = self.task_view(
            root_task_id,
            recent_event_limit=recent_event_limit,
        )
        if view is None:
            raise KeyError(f"未知 task_id: {root_task_id}")
        return {
            **view.to_dict(),
            "children": [
                self.task_tree_view(
                    child.task_id,
                    recent_event_limit=recent_event_limit,
                )
                for child in self.children_of(root_task_id)
            ],
        }

    def execution_graph(self) -> dict[str, Any]:
        nodes = [record.to_dict() for record in self._records.values()]
        edges: list[dict[str, str]] = []
        for record in self._records.values():
            if record.parent_task_id is not None:
                edges.append(
                    {
                        "type": "parent",
                        "from": record.parent_task_id,
                        "to": record.task_id,
                    }
                )
            depends_on = record.metadata.get("depends_on_task_ids", [])
            if isinstance(depends_on, list):
                for dependency in depends_on:
                    if isinstance(dependency, str):
                        edges.append(
                            {
                                "type": "dependency",
                                "from": dependency,
                                "to": record.task_id,
                            }
                        )
        return {"nodes": nodes, "edges": edges}

    async def _start_record(
        self,
        agent_id: str,
        task: str,
        *,
        input_context: dict[str, Any] | None = None,
        parent_context: AgentContext | None = None,
        parent_task_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentTaskRecord:
        child_task_id = task_id or str(uuid.uuid4())
        if child_task_id in self._records:
            raise ValueError(f"task_id 已存在: {child_task_id}")
        child_run_id = str(uuid.uuid4())
        resolved_parent_task_id = parent_task_id
        if resolved_parent_task_id is None and parent_context is not None:
            value = parent_context.metadata.get("task_id")
            resolved_parent_task_id = value if isinstance(value, str) else None

        depth = 0
        if resolved_parent_task_id is not None:
            parent = self._records.get(resolved_parent_task_id)
            if parent is None:
                raise ValueError(
                    f"parent task 不存在: {resolved_parent_task_id}"
                )
            depth = parent.depth + 1
            if depth > self.max_task_depth:
                raise RuntimeError(
                    "子 Agent 深度超过上限: "
                    f"depth={depth}, max={self.max_task_depth}"
                )
            if parent.status not in {
                AgentTaskStatus.PENDING,
                AgentTaskStatus.RUNNING,
            }:
                raise RuntimeError(
                    "parent task 已进入终态，不能创建子任务: "
                    f"{resolved_parent_task_id}"
                )
            child_count = sum(
                record.parent_task_id == resolved_parent_task_id
                for record in self._records.values()
            )
            if child_count >= self.max_children_per_task:
                raise RuntimeError(
                    "父任务子 Agent 数量达到上限: "
                    f"parent={resolved_parent_task_id}, "
                    f"max={self.max_children_per_task}"
                )

        record = AgentTaskRecord(
            task_id=child_task_id,
            agent_id=agent_id,
            task=task,
            parent_task_id=resolved_parent_task_id,
            parent_run_id=parent_context.run_id if parent_context else None,
            depth=depth,
            run_id=child_run_id,
            input_context=input_context or {},
            metadata=metadata or {},
        )
        self._records[child_task_id] = record
        self._run_to_task[child_run_id] = child_task_id
        await self._emit(record, AgentTaskEventType.TASK_CREATED)

        self._prune_records()
        return record

    async def _run_background_record(
        self,
        worker: AgentWorkerSpec,
        record: AgentTaskRecord,
        *,
        parent_context: AgentContext | None,
        session_environment: SessionEnvironment | None,
        session_policy: SessionPolicy | None,
        metadata: dict[str, Any] | None,
    ) -> AgentTaskResult:
        return await self._execute_record(
            worker,
            record,
            parent_context=parent_context,
            session_environment=session_environment,
            session_policy=session_policy,
            metadata=metadata,
            raise_on_cancel=False,
            raise_on_error=False,
        )

    async def _execute_record(
        self,
        worker: AgentWorkerSpec,
        record: AgentTaskRecord,
        *,
        parent_context: AgentContext | None,
        session_environment: SessionEnvironment | None,
        session_policy: SessionPolicy | None,
        metadata: dict[str, Any] | None,
        raise_on_cancel: bool,
        raise_on_error: bool,
    ) -> AgentTaskResult:
        execution_reserved = False
        try:
            self._reserve_execution_slot()
            execution_reserved = True
            record.status = AgentTaskStatus.RUNNING
            record.updated_at = time.time()
            await self._emit(record, AgentTaskEventType.TASK_STARTED)
            run = worker.runtime.run(
                record.task,
                run_id=record.run_id,
                agent_id=record.agent_id,
                session_environment=self._resolve_environment(
                    session_environment,
                    parent_context,
                    worker,
                ),
                session_policy=session_policy or worker.session_policy,
                metadata={
                    **worker.metadata,
                    **(metadata or {}),
                    "task_id": record.task_id,
                    "parent_task_id": record.parent_task_id,
                    "parent_run_id": record.parent_run_id,
                    "task_depth": record.depth,
                    "input_context": record.input_context,
                },
            )
            if self.task_timeout_s is None:
                result = await run
            else:
                result = await asyncio.wait_for(
                    run,
                    timeout=self.task_timeout_s,
                )
        except asyncio.CancelledError:
            task_result = AgentTaskResult(
                task_id=record.task_id,
                agent_id=record.agent_id,
                run_id=record.run_id or "",
                status=AgentTaskStatus.CANCELLED,
                final_answer=None,
                error="Agent task 已取消",
            )
            record.status = task_result.status
            record.result = task_result
            record.error = task_result.error
            record.updated_at = time.time()
            await self._emit_result(record, task_result)
            if raise_on_cancel:
                raise
            return task_result
        except asyncio.TimeoutError:
            task_result = AgentTaskResult(
                task_id=record.task_id,
                agent_id=record.agent_id,
                run_id=record.run_id or "",
                status=AgentTaskStatus.FAILED,
                final_answer=None,
                error=(
                    "Agent task 超过 Orchestrator 总超时: "
                    f"{self.task_timeout_s}s"
                ),
                output={"reason": "timeout"},
            )
            record.status = task_result.status
            record.result = task_result
            record.error = task_result.error
            record.updated_at = time.time()
            await self._emit_result(record, task_result)
            if raise_on_error:
                raise TimeoutError(task_result.error)
            return task_result
        except Exception as exc:
            task_result = AgentTaskResult(
                task_id=record.task_id,
                agent_id=record.agent_id,
                run_id=record.run_id or "",
                status=AgentTaskStatus.FAILED,
                final_answer=None,
                error=str(exc),
            )
            record.status = task_result.status
            record.result = task_result
            record.error = task_result.error
            record.updated_at = time.time()
            await self._emit_result(record, task_result)
            if raise_on_error:
                raise
            return task_result
        else:
            task_result = self._to_task_result(record, result)
            record.status = task_result.status
            record.result = task_result
            record.error = task_result.error
            record.updated_at = time.time()
            await self._emit_result(record, task_result)
            self._prune_records()
            return task_result
        finally:
            if execution_reserved:
                self._active_executions -= 1
            await self._cancel_owned_background_tasks(record.task_id)

    def _require_worker(self, agent_id: str) -> AgentWorkerSpec:
        worker = self._workers.get(agent_id)
        if worker is None:
            raise ValueError(f"未注册 agent: {agent_id}")
        return worker

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise RuntimeError("AgentOrchestrator 正在关闭或已关闭")

    def _reserve_execution_slot(self) -> None:
        if self._active_executions >= self.max_concurrent_tasks:
            raise RuntimeError(
                "Agent task 并发达到上限: "
                f"max={self.max_concurrent_tasks}"
            )
        self._active_executions += 1

    def _descendant_task_ids(self, task_id: str) -> list[str]:
        descendants: list[str] = []
        pending = [task_id]
        while pending:
            parent_id = pending.pop()
            children = [
                record.task_id
                for record in self._records.values()
                if record.parent_task_id == parent_id
            ]
            descendants.extend(children)
            pending.extend(children)
        return descendants

    async def _cancel_owned_background_tasks(self, task_id: str) -> None:
        current_task = asyncio.current_task()
        tasks = [
            self._background_tasks[descendant_id]
            for descendant_id in self._descendant_task_ids(task_id)
            if descendant_id in self._background_tasks
            and self._background_tasks[descendant_id] is not current_task
            and not self._background_tasks[descendant_id].done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _mark_record_cancelled(
        record: AgentTaskRecord,
    ) -> AgentTaskResult:
        result = AgentTaskResult(
            task_id=record.task_id,
            agent_id=record.agent_id,
            run_id=record.run_id or "",
            status=AgentTaskStatus.CANCELLED,
            final_answer=None,
            error="Agent task 已取消",
        )
        record.status = result.status
        record.result = result
        record.error = result.error
        record.updated_at = time.time()
        return result

    async def _emit_result(
        self,
        record: AgentTaskRecord,
        result: AgentTaskResult,
    ) -> None:
        if record.task_id in self._terminal_event_task_ids:
            return
        self._terminal_event_task_ids.add(record.task_id)
        event_type = AgentTaskEventType.TASK_COMPLETED
        if result.status == AgentTaskStatus.CANCELLED:
            event_type = AgentTaskEventType.TASK_CANCELLED
        elif result.status == AgentTaskStatus.FAILED:
            event_type = AgentTaskEventType.TASK_FAILED
        elif result.status == AgentTaskStatus.PARTIAL:
            event_type = AgentTaskEventType.TASK_NEEDS_INPUT
        payload = {
            "status": result.status.value,
            "error": result.error,
            "final_answer": result.final_answer,
        }
        if result.output.get("reason") is not None:
            payload["reason"] = result.output["reason"]
        if result.status == AgentTaskStatus.PARTIAL:
            payload.update(
                {
                    "question": "子 Agent 达到执行边界，是否允许继续执行？",
                    "options": [
                        {"value": "continue", "label": "继续"},
                        {"value": "stop", "label": "停止"},
                    ],
                    "default": "continue",
                    "boundary": result.output.get("termination_boundary"),
                    "checkpoint": result.output.get("checkpoint"),
                    "reason": result.output.get("error_info"),
                }
            )
        await self._emit(
            record,
            event_type,
            payload,
        )

    async def _emit(
        self,
        record: AgentTaskRecord,
        event_type: AgentTaskEventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = AgentTaskEvent(
            event_type=event_type,
            task_id=record.task_id,
            agent_id=record.agent_id,
            run_id=record.run_id,
            parent_task_id=record.parent_task_id,
            parent_run_id=record.parent_run_id,
            status=record.status,
            payload=payload or {},
            sequence=self._next_event_sequence,
        )
        self._next_event_sequence += 1
        self._events.append(event)
        self._prune_events()
        for watcher in list(self._event_watchers):
            if not watcher.matches(event):
                continue
            if watcher.queue.full():
                dropped = watcher.queue.get_nowait()
                assert dropped is not None
                watcher.queue.put_nowait(
                    replace(
                        event,
                        dropped_events=dropped.dropped_events + 1,
                    )
                )
            else:
                watcher.queue.put_nowait(event)
        if self.event_handler is not None:
            try:
                delivery = self.event_handler(event)
                if self.event_handler_timeout_s is None:
                    await delivery
                else:
                    await asyncio.wait_for(
                        delivery,
                        timeout=self.event_handler_timeout_s,
                    )
            except Exception as exc:
                self._event_delivery_failures.append(
                    EventDeliveryFailure(
                        subscriber="orchestrator",
                        event_id=event.event_id,
                        exception_type=type(exc).__name__,
                        error=str(exc),
                    )
                )

    def _prune_events(self) -> None:
        overflow = len(self._events) - self.max_events
        if overflow > 0:
            del self._events[:overflow]

    def _prune_records(self) -> None:
        overflow = len(self._records) - self.max_records
        if overflow <= 0:
            return
        evictable = [
            record
            for record in sorted(
                self._records.values(),
                key=lambda item: item.updated_at,
            )
            if record.task_id not in self._background_tasks
            and record.status
            not in {AgentTaskStatus.PENDING, AgentTaskStatus.RUNNING}
        ]
        for record in evictable[:overflow]:
            self._records.pop(record.task_id, None)
            self._terminal_event_task_ids.discard(record.task_id)
            if record.run_id is not None:
                self._run_to_task.pop(record.run_id, None)

    @staticmethod
    def _resolve_environment(
        override: SessionEnvironment | None,
        parent_context: AgentContext | None,
        worker: AgentWorkerSpec,
    ) -> SessionEnvironment | None:
        if override is not None:
            return override
        if parent_context is not None:
            return parent_context.session_environment
        return worker.session_environment

    @staticmethod
    def _to_task_result(
        record: AgentTaskRecord,
        result: AgentResult,
    ) -> AgentTaskResult:
        if result.outcome.value in {item.value for item in AgentTaskStatus}:
            status = AgentTaskStatus(result.outcome.value)
        elif result.error is not None:
            status = AgentTaskStatus.FAILED
        else:
            status = AgentTaskStatus.COMPLETED
        return AgentTaskResult(
            task_id=record.task_id,
            agent_id=record.agent_id,
            run_id=result.run_id,
            status=status,
            final_answer=result.final_answer,
            error=result.error,
            evidence_ids=tuple(
                evidence.evidence_id for evidence in result.evidence
            ),
            output={
                "state": result.state.value,
                "outcome": result.outcome.value,
                "steps": result.steps,
                "error_info": (
                    result.error_info.to_dict()
                    if result.error_info
                    else None
                ),
                "termination_boundary": (
                    result.termination_boundary.to_dict()
                    if result.termination_boundary
                    else None
                ),
                "checkpoint": (
                    result.checkpoint.to_dict()
                    if result.checkpoint
                    else None
                ),
            },
        )
