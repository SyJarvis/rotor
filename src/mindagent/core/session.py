from __future__ import annotations

import asyncio
import copy
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .conversation import ConversationStore
from .context import (
    ActionType,
    AgentContext,
    AgentResult,
    ContinuationCheckpoint,
    Evidence,
    RunOutcome,
    TaskState,
)
from .event import AgentEvent, AgentState


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return copy.deepcopy(value)


class SessionState(str, Enum):
    CREATED = "created"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class SessionCommandType(str, Enum):
    START_RUN = "start_run"
    CONTINUE_RUN = "continue_run"
    APPEND_USER_MESSAGE = "append_user_message"
    SUBMIT_USER_INPUT = "submit_user_input"
    CANCEL_RUN = "cancel_run"
    CLOSE_SESSION = "close_session"
    RUN_FINISHED = "run_finished"


class SessionEventType(str, Enum):
    SESSION_READY = "session_ready"
    RUN_ACCEPTED = "run_accepted"
    AGENT_EVENT = "agent_event"
    USER_MESSAGE_APPENDED = "user_message_appended"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_COMPLETED = "run_completed"
    CONTINUATION_REQUIRED = "continuation_required"
    COMMAND_REJECTED = "command_rejected"
    SESSION_CLOSED = "session_closed"


@dataclass(frozen=True)
class SessionEnvironment:
    workspace_root: Path | None = None
    working_directory: Path | None = None
    env_vars: Mapping[str, str] = field(default_factory=dict)
    artifact_namespace: str | None = None
    memory_namespace: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        root = (
            self.workspace_root.expanduser().resolve()
            if self.workspace_root is not None
            else None
        )
        if root is not None and not root.is_dir():
            raise ValueError(f"workspace 不存在或不是目录: {root}")

        working_directory = self.working_directory
        if working_directory is not None:
            candidate = working_directory.expanduser()
            if not candidate.is_absolute() and root is not None:
                candidate = root / candidate
            working_directory = candidate.resolve()
            if not working_directory.is_dir():
                raise ValueError(
                    f"working_directory 不存在或不是目录: "
                    f"{working_directory}"
                )
            if root is not None:
                try:
                    working_directory.relative_to(root)
                except ValueError as exc:
                    raise ValueError(
                        "working_directory 超出 session workspace"
                    ) from exc
        elif root is not None:
            working_directory = root

        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "working_directory", working_directory)
        object.__setattr__(
            self,
            "env_vars",
            MappingProxyType(dict(self.env_vars)),
        )
        object.__setattr__(
            self,
            "metadata",
            _freeze(self.metadata),
        )


@dataclass(frozen=True)
class SessionPolicy:
    allowed_action_types: frozenset[ActionType] | None = None
    allowed_tools: frozenset[str] | None = None
    allow_network: bool = False
    allow_write: bool = True
    allow_dangerous: bool = False
    max_parallel_actions: int = 1

    def __post_init__(self) -> None:
        if self.max_parallel_actions < 1:
            raise ValueError("max_parallel_actions 必须大于 0")
        if self.allowed_action_types is not None:
            object.__setattr__(
                self,
                "allowed_action_types",
                frozenset(self.allowed_action_types),
            )
        if self.allowed_tools is not None:
            object.__setattr__(
                self,
                "allowed_tools",
                frozenset(self.allowed_tools),
            )


@dataclass(frozen=True)
class SessionCommand:
    command_type: SessionCommandType
    payload: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class SessionEvent:
    session_id: str
    event_type: SessionEventType
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    correlation_id: str | None = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    agent_event: AgentEvent | None = None
    sequence: int = 0
    dropped_events: int = 0


@dataclass
class _PendingContinuation:
    checkpoint: ContinuationCheckpoint
    task_state: TaskState
    evidence: list[Evidence]
    agent_id: str


@dataclass
class _PendingInterrupt:
    command: SessionCommand


class SessionRuntime(Protocol):
    async def run_context(self, context: AgentContext) -> AgentResult: ...

    def cancel(self, run_id: str) -> bool: ...

    def interrupt(self, run_id: str, reason: str) -> bool: ...

    def respond_to_user(self, run_id: str, response: str) -> bool: ...

    def _register_session_handler(self, session_id: str, handler: Any) -> None:
        ...

    def _unregister_session_handler(self, session_id: str) -> None: ...


class AgentSession:
    def __init__(
        self,
        runtime: SessionRuntime,
        *,
        session_id: str | None = None,
        environment: SessionEnvironment | None = None,
        policy: SessionPolicy | None = None,
        conversation_store: ConversationStore | None = None,
        command_queue_size: int = 100,
        event_queue_size: int = 1000,
    ):
        if command_queue_size < 1 or event_queue_size < 1:
            raise ValueError("session queue size 必须大于 0")
        self.runtime = runtime
        self.session_id = session_id or str(uuid.uuid4())
        self.environment = environment or SessionEnvironment()
        self.policy = policy or SessionPolicy()
        self.conversation_store = conversation_store
        self.state = SessionState.CREATED
        self.command_queue: asyncio.Queue[SessionCommand] = asyncio.Queue(
            maxsize=command_queue_size
        )
        self.event_queue: asyncio.Queue[SessionEvent] = asyncio.Queue(
            maxsize=event_queue_size
        )
        self._messages: list[dict[str, Any]] = []
        self._artifacts: dict[str, Any] = {}
        self._actor_task: asyncio.Task[None] | None = None
        self._active_run_task: asyncio.Task[AgentResult] | None = None
        self._active_context: AgentContext | None = None
        self._active_command_id: str | None = None
        self._pending_continuation: _PendingContinuation | None = None
        self._pending_interrupt: _PendingInterrupt | None = None
        self._closed = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._next_event_sequence = 1

    @property
    def active_run_id(self) -> str | None:
        if self._active_context is None:
            return None
        return self._active_context.run_id

    @property
    def messages(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._messages)

    @property
    def artifacts(self) -> dict[str, Any]:
        return copy.deepcopy(self._artifacts)

    @property
    def pending_checkpoint(self) -> ContinuationCheckpoint | None:
        if self._pending_continuation is None:
            return None
        return copy.deepcopy(self._pending_continuation.checkpoint)

    async def start(self) -> None:
        if self.state == SessionState.CLOSED:
            raise RuntimeError("session 已关闭")
        if self._actor_task is not None:
            return
        if self.conversation_store is not None:
            self._messages = self.conversation_store.load(
                self.session_id
            )
        self.runtime._register_session_handler(
            self.session_id,
            self._handle_agent_event,
        )
        self._actor_task = asyncio.create_task(
            self._run_loop(),
            name=f"mindagent-session-{self.session_id}",
        )
        self.state = SessionState.READY
        await self._publish(SessionEventType.SESSION_READY)

    async def submit(self, command: SessionCommand) -> str:
        await self.start()
        if self.state in {SessionState.CLOSING, SessionState.CLOSED}:
            raise RuntimeError("session 正在关闭或已关闭")
        await self.command_queue.put(command)
        return command.command_id

    async def submit_run(
        self,
        user_input: str,
        *,
        run_id: str | None = None,
        agent_id: str = "default_agent",
        metadata: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
    ) -> str:
        return await self.submit(
            SessionCommand(
                SessionCommandType.START_RUN,
                {
                    "user_input": user_input,
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "metadata": metadata or {},
                    "artifacts": artifacts or {},
                },
            )
        )

    async def submit_user_input(self, run_id: str, response: str) -> str:
        return await self.submit(
            SessionCommand(
                SessionCommandType.SUBMIT_USER_INPUT,
                {"run_id": run_id, "response": response},
            )
        )

    async def append_user_message(
        self,
        user_input: str,
        *,
        run_id: str | None = None,
        agent_id: str = "default_agent",
        metadata: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
    ) -> str:
        return await self.submit(
            SessionCommand(
                SessionCommandType.APPEND_USER_MESSAGE,
                {
                    "user_input": user_input,
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "metadata": metadata or {},
                    "artifacts": artifacts or {},
                },
            )
        )

    async def continue_run(
        self,
        checkpoint_id: str,
        *,
        user_input: str | None = None,
        run_id: str | None = None,
    ) -> str:
        return await self.submit(
            SessionCommand(
                SessionCommandType.CONTINUE_RUN,
                {
                    "checkpoint_id": checkpoint_id,
                    "user_input": user_input,
                    "run_id": run_id,
                },
            )
        )

    async def cancel_run(self, run_id: str) -> str:
        await self.start()
        command = SessionCommand(
            SessionCommandType.CANCEL_RUN,
            {"run_id": run_id},
        )
        await self._cancel_run(command)
        return command.command_id

    async def next_event(self) -> SessionEvent:
        return await self.event_queue.get()

    async def close(self) -> None:
        async with self._close_lock:
            if self.state == SessionState.CLOSED:
                return
            await self._shutdown(str(uuid.uuid4()))

    async def _run_loop(self) -> None:
        try:
            while True:
                command = await self.command_queue.get()
                if command.command_type == SessionCommandType.START_RUN:
                    await self._start_run(command)
                elif command.command_type == SessionCommandType.CONTINUE_RUN:
                    await self._continue_run(command)
                elif command.command_type == SessionCommandType.APPEND_USER_MESSAGE:
                    await self._append_user_message(command)
                elif command.command_type == SessionCommandType.SUBMIT_USER_INPUT:
                    await self._submit_user_input(command)
                elif command.command_type == SessionCommandType.CANCEL_RUN:
                    await self._cancel_run(command)
                elif command.command_type == SessionCommandType.RUN_FINISHED:
                    await self._finish_run(command)
                elif command.command_type == SessionCommandType.CLOSE_SESSION:
                    await self._close(command)
                    return
        except Exception as exc:
            self.state = SessionState.FAILED
            await self._publish(
                SessionEventType.COMMAND_REJECTED,
                payload={
                    "reason": str(exc),
                    "exception_type": type(exc).__name__,
                },
            )
            raise
        finally:
            self.runtime._unregister_session_handler(self.session_id)
            self._closed.set()

    async def _start_run(self, command: SessionCommand) -> None:
        if self._active_run_task is not None:
            await self._reject(command, "同一 session 只能有一个活跃 run")
            return
        payload = command.payload
        artifacts = copy.deepcopy(self._artifacts)
        artifacts.update(copy.deepcopy(payload["artifacts"]))
        context_kwargs = {
            "user_input": payload["user_input"],
            "session_id": self.session_id,
            "agent_id": payload["agent_id"],
            "session_environment": self.environment,
            "session_policy": self.policy,
            "messages": copy.deepcopy(self._messages),
            "metadata": copy.deepcopy(payload["metadata"]),
            "artifacts": artifacts,
        }
        if payload["run_id"] is not None:
            context_kwargs["run_id"] = payload["run_id"]
        context = AgentContext(**context_kwargs)
        self._pending_continuation = None
        await self._launch_run(context, command)

    async def _append_user_message(self, command: SessionCommand) -> None:
        payload = command.payload
        user_input = payload["user_input"]
        if not isinstance(user_input, str) or not user_input.strip():
            await self._reject(command, "追加消息不能为空")
            return
        if self._active_context is None:
            await self._start_run(command)
            return
        if self._pending_interrupt is not None:
            await self._reject(command, "已有待处理的追加消息中断")
            return

        run_id = self._active_context.run_id
        if not self.runtime.interrupt(
            run_id,
            "用户追加消息，中断当前 run",
        ):
            await self._reject(command, f"无法中断 run: {run_id}")
            return

        self._pending_interrupt = _PendingInterrupt(command=command)
        await self._publish(
            SessionEventType.USER_MESSAGE_APPENDED,
            run_id=run_id,
            correlation_id=command.command_id,
            payload={
                "user_input": user_input,
                "interrupted_run_id": run_id,
            },
        )

    async def _continue_run(self, command: SessionCommand) -> None:
        if self._active_run_task is not None:
            await self._reject(command, "同一 session 只能有一个活跃 run")
            return
        pending = self._pending_continuation
        if pending is None:
            await self._reject(command, "当前 session 没有待继续的 run")
            return
        payload = command.payload
        if payload["checkpoint_id"] != pending.checkpoint.checkpoint_id:
            await self._reject(command, "checkpoint_id 不匹配")
            return
        user_input = payload["user_input"] or pending.checkpoint.next_step
        context_kwargs = {
            "user_input": user_input,
            "session_id": self.session_id,
            "agent_id": pending.agent_id,
            "session_environment": self.environment,
            "session_policy": self.policy,
            "messages": copy.deepcopy(self._messages),
            "metadata": {
                "_continuation_checkpoint": pending.checkpoint,
                "continuation_source_run_id": (
                    pending.checkpoint.source_run_id
                ),
            },
            "artifacts": copy.deepcopy(self._artifacts),
            "task_state": copy.deepcopy(pending.task_state),
            "evidence": copy.deepcopy(pending.evidence),
        }
        if payload["run_id"] is not None:
            context_kwargs["run_id"] = payload["run_id"]
        context = AgentContext(**context_kwargs)
        self._pending_continuation = None
        await self._launch_run(context, command)

    async def _launch_run(
        self,
        context: AgentContext,
        command: SessionCommand,
    ) -> None:
        self._active_context = context
        self._active_command_id = command.command_id
        self.state = SessionState.RUNNING
        self._active_run_task = asyncio.create_task(
            self.runtime.run_context(context),
            name=f"mindagent-run-{context.run_id}",
        )
        self._active_run_task.add_done_callback(
            lambda task: asyncio.create_task(
                self.command_queue.put(
                    SessionCommand(
                        SessionCommandType.RUN_FINISHED,
                        {"task": task, "context": context},
                        correlation_id=command.command_id,
                    )
                )
            )
        )
        await self._publish(
            SessionEventType.RUN_ACCEPTED,
            run_id=context.run_id,
            correlation_id=command.command_id,
        )

    async def _submit_user_input(self, command: SessionCommand) -> None:
        run_id = command.payload["run_id"]
        if run_id != self.active_run_id:
            await self._reject(command, f"run 不属于当前 session: {run_id}")
            return
        if not self.runtime.respond_to_user(
            run_id,
            command.payload["response"],
        ):
            await self._reject(command, "当前 run 不接受用户输入")

    async def _cancel_run(self, command: SessionCommand) -> None:
        run_id = command.payload["run_id"]
        if run_id != self.active_run_id or not self.runtime.cancel(run_id):
            await self._reject(command, f"无法取消 run: {run_id}")

    async def _finish_run(self, command: SessionCommand) -> None:
        task = command.payload["task"]
        context = command.payload["context"]
        pending_interrupt = self._pending_interrupt
        result: AgentResult | None = None
        try:
            result = task.result()
        except Exception as exc:
            await self._publish(
                SessionEventType.RUN_COMPLETED,
                run_id=context.run_id,
                correlation_id=command.correlation_id,
                payload={
                    "result": None,
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                },
            )
        else:
            self._messages = copy.deepcopy(context.messages)
            self._artifacts = copy.deepcopy(context.artifacts)
            conversation_persist_error = None
            if self.conversation_store is not None:
                try:
                    self.conversation_store.save(
                        self.session_id,
                        self._messages,
                    )
                except Exception as exc:
                    conversation_persist_error = str(exc)
            if (
                result.outcome == RunOutcome.PARTIAL
                and result.checkpoint is not None
            ):
                self._pending_continuation = _PendingContinuation(
                    checkpoint=result.checkpoint,
                    task_state=copy.deepcopy(result.task_state),
                    evidence=copy.deepcopy(result.evidence),
                    agent_id=context.agent_id,
                )
            else:
                self._pending_continuation = None
            await self._publish(
                SessionEventType.RUN_COMPLETED,
                run_id=context.run_id,
                correlation_id=command.correlation_id,
                payload={
                    "result": result,
                    "conversation_persist_error": (
                        conversation_persist_error
                    ),
                },
            )
            if self._pending_continuation is not None:
                checkpoint = self._pending_continuation.checkpoint
                await self._publish(
                    SessionEventType.CONTINUATION_REQUIRED,
                    run_id=context.run_id,
                    correlation_id=command.correlation_id,
                    payload={
                        "checkpoint": checkpoint.to_dict(),
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "boundary": checkpoint.boundary.to_dict(),
                        "progress_summary": (
                            checkpoint.progress_summary
                        ),
                        "suggested_options": list(
                            checkpoint.boundary.suggested_options
                        ),
                    },
                )
        finally:
            self._active_run_task = None
            self._active_context = None
            self._active_command_id = None
            self.state = SessionState.READY
        if pending_interrupt is not None:
            self._pending_interrupt = None
            await self._publish(
                SessionEventType.RUN_INTERRUPTED,
                run_id=context.run_id,
                correlation_id=pending_interrupt.command.command_id,
                payload={
                    "interrupted_run_id": context.run_id,
                    "outcome": result.outcome.value if result else None,
                },
            )
            await self._start_run(pending_interrupt.command)

    async def _close(self, command: SessionCommand) -> None:
        await self._shutdown(command.command_id)

    async def _shutdown(self, correlation_id: str) -> None:
        self.state = SessionState.CLOSING
        if self._active_context is not None:
            self.runtime.cancel(self._active_context.run_id)
        if self._active_run_task is not None:
            await asyncio.gather(
                self._active_run_task,
                return_exceptions=True,
            )
        current_task = asyncio.current_task()
        if (
            self._actor_task is not None
            and self._actor_task is not current_task
            and not self._actor_task.done()
        ):
            self._actor_task.cancel()
            await asyncio.gather(self._actor_task, return_exceptions=True)
        self.state = SessionState.CLOSED
        await self._publish(
            SessionEventType.SESSION_CLOSED,
            correlation_id=correlation_id,
        )
        self._closed.set()

    async def _handle_agent_event(self, event: AgentEvent) -> None:
        if event.run_id != self.active_run_id:
            return
        if event.state == AgentState.WAITING_FOR_USER:
            self.state = SessionState.WAITING
        elif self._active_run_task is not None:
            self.state = SessionState.RUNNING
        await self._publish(
            SessionEventType.AGENT_EVENT,
            run_id=event.run_id,
            agent_event=event,
        )

    async def _reject(self, command: SessionCommand, reason: str) -> None:
        await self._publish(
            SessionEventType.COMMAND_REJECTED,
            correlation_id=command.command_id,
            payload={"reason": reason},
        )

    async def _publish(
        self,
        event_type: SessionEventType,
        *,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        correlation_id: str | None = None,
        agent_event: AgentEvent | None = None,
    ) -> None:
        event = SessionEvent(
            session_id=self.session_id,
            event_type=event_type,
            payload=payload or {},
            run_id=run_id,
            correlation_id=correlation_id,
            agent_event=agent_event,
            sequence=self._next_event_sequence,
        )
        self._next_event_sequence += 1
        if self.event_queue.full():
            dropped = self.event_queue.get_nowait()
            event = SessionEvent(
                session_id=event.session_id,
                event_type=event.event_type,
                payload=event.payload,
                run_id=event.run_id,
                correlation_id=event.correlation_id,
                event_id=event.event_id,
                timestamp=event.timestamp,
                agent_event=event.agent_event,
                sequence=event.sequence,
                dropped_events=dropped.dropped_events + 1,
            )
        self.event_queue.put_nowait(event)
