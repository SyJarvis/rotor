from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import (
    ActionRequest,
    ActionRisk,
    ActionType,
    AgentError,
    AgentContext,
    AgentResult,
    AgentRunStatus,
    BoundaryReason,
    BoundaryScope,
    ContinuationCheckpoint,
    Evidence,
    CompletionClaim,
    CompletionValidationResult,
    ErrorCode,
    ErrorPhase,
    ExecutionBoundary,
    RunOutcome,
    UserInputRequest,
)
from .conversation import ConversationStore
from .contracts import (
    ActionExecutor,
    ContextManager,
    CompletionGate,
    EventHandler,
    HumanApprovalHandler,
    PolicyEngine,
    Reasoner,
    ValidationResult,
)
from .dispatcher import ActionDispatcher
from .event import AgentEvent, AgentState, EventDeliveryFailure, EventType
from .heartbeat import Heartbeat
from .lifecycle import CleanupFailure, CloseReport, wait_for_cleanup
from .react_loop import ReActLoop, StepTimeoutError
from .scheduler import ActionScheduler
from .session import (
    AgentSession,
    SessionEnvironment,
    SessionPolicy,
)
from .state_machine import AgentStateMachine


@dataclass
class RunConfig:
    max_steps: int = 8
    step_timeout_s: float | None = 60.0
    total_timeout_s: float | None = 300.0
    heartbeat_interval_s: float = 2.0
    allow_parallel_actions: bool = True
    max_parallel_actions: int = 4
    action_timeout_s: float | None = 60.0
    batch_timeout_s: float | None = 120.0
    allow_write_actions: bool = True
    allow_dangerous_actions: bool = False
    enable_heartbeat: bool = True
    chatml_dir: str | None = None
    event_handler_timeout_s: float | None = 5.0

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps 必须大于 0")
        if self.step_timeout_s is not None and self.step_timeout_s <= 0:
            raise ValueError("step_timeout_s 必须大于 0")
        if self.total_timeout_s is not None and self.total_timeout_s <= 0:
            raise ValueError("total_timeout_s 必须大于 0")
        if self.heartbeat_interval_s <= 0:
            raise ValueError("heartbeat_interval_s 必须大于 0")
        if self.max_parallel_actions < 1:
            raise ValueError("max_parallel_actions 必须大于 0")
        if self.action_timeout_s is not None and self.action_timeout_s <= 0:
            raise ValueError("action_timeout_s 必须大于 0")
        if self.batch_timeout_s is not None and self.batch_timeout_s <= 0:
            raise ValueError("batch_timeout_s 必须大于 0")
        if (
            self.event_handler_timeout_s is not None
            and self.event_handler_timeout_s <= 0
        ):
            raise ValueError("event_handler_timeout_s 必须大于 0")


class DefaultPolicyEngine:
    def __init__(self, config: RunConfig):
        self.config = config

    async def validate_action(
        self,
        context: AgentContext,
        action: ActionRequest,
    ) -> ValidationResult:
        session_policy = context.session_policy
        if session_policy is not None:
            if (
                session_policy.allowed_action_types is not None
                and action.action_type
                not in session_policy.allowed_action_types
            ):
                return ValidationResult(
                    False,
                    "当前 session 不允许该 action 类型",
                )
            if (
                session_policy.allowed_tools is not None
                and action.name not in session_policy.allowed_tools
            ):
                return ValidationResult(
                    False,
                    f"当前 session 不允许工具: {action.name}",
                )
            if action.risk == ActionRisk.WRITE and not session_policy.allow_write:
                return ValidationResult(
                    False,
                    "当前 session 不允许写操作",
                )
            if (
                action.risk == ActionRisk.DANGEROUS
                and not session_policy.allow_dangerous
            ):
                return ValidationResult(
                    False,
                    "当前 session 不允许危险操作",
                )

        if action.risk == ActionRisk.WRITE and not self.config.allow_write_actions:
            return ValidationResult(False, "当前配置不允许写操作")

        if action.risk == ActionRisk.DANGEROUS:
            if not self.config.allow_dangerous_actions:
                return ValidationResult(False, "当前配置不允许危险操作")
            return ValidationResult(True, "危险操作需要人工确认", True)

        if action.require_human_approval:
            return ValidationResult(True, "该 action 显式要求人工确认", True)

        return ValidationResult(True, "action 通过默认策略校验")


class DefaultCompletionGate:
    async def validate(
        self,
        context: AgentContext,
        claim: CompletionClaim,
    ) -> CompletionValidationResult:
        task_state = context.task_state
        if task_state is None:
            return CompletionValidationResult(
                False,
                ("缺少 TaskState",),
            )

        criteria = {
            item.criterion_id: item
            for item in task_state.acceptance_criteria
        }
        reasons: list[str] = []
        unknown_ids = set(claim.criterion_evidence) - set(criteria)
        if unknown_ids:
            reasons.append(
                "CompletionClaim 包含未知验收标准: "
                + ", ".join(sorted(unknown_ids))
            )

        available_refs = {
            item.evidence_id: item
            for item in context.evidence
        }
        for criterion_id, criterion in criteria.items():
            evidence_refs = claim.criterion_evidence.get(
                criterion_id,
                [],
            )
            if not evidence_refs:
                if criterion.required:
                    reasons.append(
                        f"验收标准缺少证据: "
                        f"{criterion.description}"
                    )
                continue
            invalid_refs = [
                ref
                for ref in evidence_refs
                if ref not in available_refs
            ]
            if invalid_refs:
                reasons.append(
                    f"验收标准引用了不存在的证据 "
                    f"{criterion_id}: {', '.join(invalid_refs)}"
                )
                continue
            failed_refs = [
                ref
                for ref in evidence_refs
                if not available_refs[ref].valid
            ]
            if failed_refs:
                reasons.append(
                    f"验收标准引用了无效证据 "
                    f"{criterion_id}: {', '.join(failed_refs)}"
                )

        return CompletionValidationResult(
            accepted=not reasons,
            reasons=tuple(reasons),
        )


class SimpleContextManager:
    def __new__(cls, *args, **kwargs):
        from mindagent.context import ContextManager as DefaultContextManager

        return DefaultContextManager(*args, **kwargs)


class AgentRuntime:
    def __init__(
        self,
        reasoner: Reasoner,
        executor: ActionExecutor,
        context_manager: ContextManager | None = None,
        policy_engine: PolicyEngine | None = None,
        completion_gate: CompletionGate | None = None,
        config: RunConfig | None = None,
        event_handler: EventHandler | None = None,
        human_approval_handler: HumanApprovalHandler | None = None,
    ):
        self.config = config or RunConfig()
        self.reasoner = reasoner
        if isinstance(executor, ActionDispatcher):
            self.executor = executor
        else:
            self.executor = ActionDispatcher()
            self.executor.register(ActionType.TOOL, executor)
        self.scheduler = ActionScheduler(
            self.executor,
            allow_parallel_actions=self.config.allow_parallel_actions,
            max_parallel_actions=self.config.max_parallel_actions,
            action_timeout_s=self.config.action_timeout_s,
            batch_timeout_s=self.config.batch_timeout_s,
        )
        self.context_manager = context_manager or SimpleContextManager()
        self.policy_engine = policy_engine or DefaultPolicyEngine(self.config)
        self.completion_gate = (
            completion_gate or DefaultCompletionGate()
        )
        self.state_machine = AgentStateMachine()
        self.event_handler = event_handler
        self.human_approval_handler = human_approval_handler
        self._active_contexts: dict[str, AgentContext] = {}
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._user_input_queues: dict[str, asyncio.Queue[str]] = {}
        self._cancel_requested: set[str] = set()
        self._sessions: dict[str, AgentSession] = {}
        self._session_event_handlers: dict[str, EventHandler] = {}
        self._event_delivery_failures: deque[EventDeliveryFailure] = deque(
            maxlen=1000
        )
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._close_report: CloseReport | None = None
        self.heartbeat = Heartbeat(
            self.config.heartbeat_interval_s,
            self._emit,
        )
        self.react_loop = ReActLoop(
            reasoner=self.reasoner,
            scheduler=self.scheduler,
            context_manager=self.context_manager,
            policy_engine=self.policy_engine,
            completion_gate=self.completion_gate,
            state_machine=self.state_machine,
            emit=self._emit,
            max_steps=self.config.max_steps,
            step_timeout_s=self.config.step_timeout_s,
            human_approval_handler=self.human_approval_handler,
            user_input_handler=self._wait_for_user_input,
        )

        add_progress_handler = getattr(
            self.executor,
            "add_progress_handler",
            None,
        )
        if callable(add_progress_handler):
            add_progress_handler(self._handle_action_progress)
        add_text_delta_handler = getattr(
            self.reasoner,
            "add_text_delta_handler",
            None,
        )
        if callable(add_text_delta_handler):
            add_text_delta_handler(self._handle_text_delta)

    async def run(
        self,
        user_input: str,
        *,
        run_id: str | None = None,
        agent_id: str = "default_agent",
        metadata: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        session_id: str | None = None,
        session_environment: SessionEnvironment | None = None,
        session_policy: SessionPolicy | None = None,
    ) -> AgentResult:
        context_kwargs: dict[str, Any] = {
            "user_input": user_input,
            "session_id": session_id,
            "agent_id": agent_id,
            "session_environment": session_environment,
            "session_policy": session_policy,
            "metadata": metadata or {},
            "artifacts": artifacts or {},
        }
        if run_id is not None:
            context_kwargs["run_id"] = run_id
        return await self.run_context(AgentContext(**context_kwargs))

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def event_delivery_failures(self) -> tuple[EventDeliveryFailure, ...]:
        return tuple(self._event_delivery_failures)

    async def __aenter__(self) -> AgentRuntime:
        if self._closing or self._closed:
            raise RuntimeError("AgentRuntime 正在关闭或已关闭")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    def create_session(
        self,
        *,
        session_id: str | None = None,
        environment: SessionEnvironment | None = None,
        policy: SessionPolicy | None = None,
        conversation_store: ConversationStore | None = None,
        command_queue_size: int = 100,
        event_queue_size: int = 1000,
    ) -> AgentSession:
        if self._closing or self._closed:
            raise RuntimeError("AgentRuntime 正在关闭或已关闭")
        session = AgentSession(
            self,
            session_id=session_id,
            environment=environment,
            policy=policy,
            conversation_store=conversation_store,
            command_queue_size=command_queue_size,
            event_queue_size=event_queue_size,
        )
        if session.session_id in self._sessions:
            raise ValueError(f"session 已存在: {session.session_id}")
        self._sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str) -> AgentSession | None:
        return self._sessions.get(session_id)

    def list_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    async def close_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        await session.close()
        self._sessions.pop(session_id, None)
        return True

    async def close(
        self,
        *,
        close_dependencies: bool = False,
        timeout_s: float | None = 10.0,
    ) -> CloseReport:
        """Stop owned async work and optionally close injected dependencies."""
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
                sessions = list(self._sessions.values())
                if sessions:
                    await wait_for_cleanup(
                        {
                            f"session:{session.session_id}": session.close()
                            for session in sessions
                        },
                        deadline=deadline,
                        failures=failures,
                        remaining_tasks=remaining_tasks,
                    )
                self._sessions.clear()
                self._session_event_handlers.clear()

                current_task = asyncio.current_task()
                active_tasks = [
                    task
                    for task in self._active_tasks.values()
                    if task is not current_task and not task.done()
                ]
                for run_id in list(self._active_tasks):
                    task = self._active_tasks.get(run_id)
                    if task is not current_task:
                        self.cancel(run_id)
                if active_tasks:
                    await wait_for_cleanup(
                        {
                            f"run:{run_id}": task
                            for run_id, task in self._active_tasks.items()
                            if task in active_tasks
                        },
                        deadline=deadline,
                        failures=failures,
                        remaining_tasks=remaining_tasks,
                    )

                if close_dependencies:
                    dependencies = {
                        "dependency:executor": self._close_dependency(
                            self.executor
                        )
                    }
                    if self.reasoner is not self.executor:
                        dependencies["dependency:reasoner"] = (
                            self._close_dependency(self.reasoner)
                        )
                    await wait_for_cleanup(
                        dependencies,
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

    @staticmethod
    async def _close_dependency(dependency: Any) -> None:
        close = getattr(dependency, "close", None)
        if close is None:
            close = getattr(dependency, "aclose", None)
        if not callable(close):
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result

    async def run_context(self, context: AgentContext) -> AgentResult:
        if self._closing or self._closed:
            raise RuntimeError("AgentRuntime 正在关闭或已关闭")
        if context.state != AgentState.IDLE:
            raise ValueError("只能运行处于 idle 状态的 AgentContext")
        if context.run_id in self._active_contexts:
            raise ValueError(f"run_id 已在运行: {context.run_id}")

        self._active_contexts[context.run_id] = context
        self._user_input_queues[context.run_id] = asyncio.Queue(maxsize=1)
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("AgentRuntime 必须在 asyncio task 中运行")
        self._active_tasks[context.run_id] = task
        heartbeat_task: asyncio.Task[None] | None = None
        if self.config.enable_heartbeat:
            heartbeat_task = asyncio.create_task(self.heartbeat.run(context))

        try:
            await asyncio.wait_for(
                self.react_loop.run(context),
                timeout=self.config.total_timeout_s,
            )
        except StepTimeoutError as exc:
            context.error = str(exc)
            context.error_info = AgentError(
                code=ErrorCode.STEP_TIMEOUT,
                message=context.error,
                phase=exc.phase,
                recoverable=True,
            )
            await self._safe_set_state(context, AgentState.TIMEOUT)
            await self._emit_error(context)
        except asyncio.TimeoutError:
            context.error = "Agent run 超过超时时间"
            context.error_info = AgentError(
                code=ErrorCode.RUN_TIMEOUT,
                message=context.error,
                phase=ErrorPhase.RUNTIME,
                recoverable=True,
            )
            await self._safe_set_state(context, AgentState.TIMEOUT)
            await self._emit_error(context)
        except asyncio.CancelledError:
            context.cancelled = True
            context.error = "Agent run 已取消"
            context.error_info = AgentError(
                code=ErrorCode.CANCELLED,
                message=context.error,
                phase=ErrorPhase.RUNTIME,
            )
            await self._safe_set_state(context, AgentState.CANCELLED)
            if context.run_id not in self._cancel_requested:
                raise
        except Exception as exc:
            context.error = str(exc)
            context.error_info = AgentError(
                code=ErrorCode.INTERNAL_ERROR,
                message=context.error,
                phase=ErrorPhase.RUNTIME,
                details={"exception_type": type(exc).__name__},
            )
            await self._safe_set_state(context, AgentState.ERROR)
            await self._emit_error(context)
        finally:
            if self._boundary_from_error(context.error_info) is not None:
                self._discard_incomplete_action_batch(context)
            self._finalize_result(context)
            self._active_contexts.pop(context.run_id, None)
            self._active_tasks.pop(context.run_id, None)
            self._user_input_queues.pop(context.run_id, None)
            self._cancel_requested.discard(context.run_id)
            if heartbeat_task:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            self._save_chatml_if_configured(context)
            release_context = getattr(
                self.context_manager,
                "release_context",
                None,
            )
            if callable(release_context):
                release_context(context.run_id)

        return self._to_result(context)

    def cancel(self, run_id: str) -> bool:
        context = self._active_contexts.get(run_id)
        task = self._active_tasks.get(run_id)
        if context is None or task is None:
            return False
        context.cancelled = True
        self._cancel_requested.add(run_id)
        task.cancel()
        return True

    def interrupt(self, run_id: str, reason: str) -> bool:
        context = self._active_contexts.get(run_id)
        if context is None:
            return False
        context.metadata["_interrupt_requested"] = True
        context.metadata["_interrupt_reason"] = reason
        if context.state == AgentState.WAITING_FOR_USER:
            return self.cancel(run_id)
        return True

    def respond_to_user(self, run_id: str, response: str) -> bool:
        context = self._active_contexts.get(run_id)
        queue = self._user_input_queues.get(run_id)
        if (
            context is None
            or queue is None
            or context.state != AgentState.WAITING_FOR_USER
            or not response.strip()
            or context.metadata.get("_user_response_submitted")
        ):
            return False
        context.metadata["_user_response_submitted"] = True
        queue.put_nowait(response)
        return True

    def get_run_status(self, run_id: str) -> AgentRunStatus | None:
        context = self._active_contexts.get(run_id)
        if context is None:
            return None
        return AgentRunStatus(
            run_id=context.run_id,
            state=context.state,
            step_index=context.step_index,
            final_answer=context.final_answer,
            error=context.error,
            error_info=context.error_info,
            cancelled=context.cancelled,
            current_batch_id=(
                context.current_action_batch.batch_id
                if context.current_action_batch is not None
                else None
            ),
            current_action_ids=(
                [
                    action.action_id
                    for action in context.current_action_batch.actions
                ]
                if context.current_action_batch is not None
                else []
            ),
            current_decision_kind=(
                context.current_decision.decision_kind.value
                if context.current_decision is not None
                else None
            ),
            pending_user_question=(
                context.pending_user_request.question
                if context.pending_user_request
                else None
            ),
            created_at=context.created_at,
            updated_at=context.updated_at,
        )

    def list_active_run_ids(self) -> list[str]:
        return list(self._active_contexts.keys())

    async def _handle_action_progress(
        self,
        context: AgentContext,
        action: ActionRequest,
        progress: Any,
    ) -> None:
        await self._emit(
            context,
            EventType.ACTION_PROGRESS,
            {
                "batch_id": (
                    context.current_action_batch.batch_id
                    if context.current_action_batch
                    else None
                ),
                "action_id": action.action_id,
                "action_name": action.name,
                "message": progress.message,
                "data": progress.data,
                "progress": progress.progress,
            },
        )

    async def _handle_text_delta(
        self,
        context: AgentContext,
        delta: str,
    ) -> None:
        await self._emit(
            context,
            EventType.TEXT_DELTA,
            {"delta": delta},
        )

    async def _wait_for_user_input(
        self,
        context: AgentContext,
        request: UserInputRequest,
    ) -> str:
        queue = self._user_input_queues.get(context.run_id)
        if queue is None:
            raise RuntimeError(
                f"run_id 没有用户输入队列: {context.run_id}"
            )
        return await queue.get()

    async def _safe_set_state(
        self,
        context: AgentContext,
        new_state: AgentState,
    ) -> None:
        old_state = context.state
        try:
            self.state_machine.transition(context, new_state)
        except Exception:
            context.state = new_state
            context.updated_at = time.time()
        await self._emit(
            context,
            EventType.STATE_CHANGED,
            {"old_state": old_state.value, "new_state": new_state.value},
        )

    async def _emit_error(self, context: AgentContext) -> None:
        await self._emit(
            context,
            EventType.ERROR_CREATED,
            {
                "error": context.error,
                "error_info": (
                    context.error_info.to_dict()
                    if context.error_info
                    else None
                ),
            },
        )

    async def _emit(
        self,
        context: AgentContext,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = AgentEvent(
            run_id=context.run_id,
            event_type=event_type,
            state=context.state,
            payload=payload or {},
        )
        context.events.append(event)
        if self.event_handler:
            await self._deliver_event(
                self.event_handler,
                event,
                subscriber="runtime",
            )
        if context.session_id is not None:
            handler = self._session_event_handlers.get(
                context.session_id
            )
            if handler is not None:
                await self._deliver_event(
                    handler,
                    event,
                    subscriber=f"session:{context.session_id}",
                )

    async def _deliver_event(
        self,
        handler: EventHandler,
        event: AgentEvent,
        *,
        subscriber: str,
    ) -> None:
        try:
            delivery = handler(event)
            timeout = self.config.event_handler_timeout_s
            if timeout is None:
                await delivery
            else:
                await asyncio.wait_for(delivery, timeout=timeout)
        except Exception as exc:
            self._event_delivery_failures.append(
                EventDeliveryFailure(
                    subscriber=subscriber,
                    event_id=event.event_id,
                    exception_type=type(exc).__name__,
                    error=str(exc),
                )
            )

    def _register_session_handler(
        self,
        session_id: str,
        handler: EventHandler,
    ) -> None:
        existing = self._session_event_handlers.get(session_id)
        if existing is not None and existing is not handler:
            raise ValueError(f"session event handler 已注册: {session_id}")
        self._session_event_handlers[session_id] = handler

    def _unregister_session_handler(self, session_id: str) -> None:
        self._session_event_handlers.pop(session_id, None)

    def _save_chatml_if_configured(self, context: AgentContext) -> None:
        chatml_dir = self.config.chatml_dir
        if chatml_dir is None:
            return
        path = Path(chatml_dir) / f"{context.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                {"messages": context.messages},
                f,
                ensure_ascii=False,
                indent=2,
            )

    @classmethod
    def _finalize_result(cls, context: AgentContext) -> None:
        if context.state == AgentState.FINAL:
            context.outcome = RunOutcome.FINAL
            return
        if context.state == AgentState.CANCELLED:
            context.outcome = RunOutcome.CANCELLED
            return

        boundary = cls._boundary_from_error(context.error_info)
        if boundary is not None:
            context.termination_boundary = boundary
            context.checkpoint = cls._create_checkpoint(
                context,
                boundary,
            )
            context.outcome = RunOutcome.PARTIAL
            return
        context.outcome = RunOutcome.ERROR

    @staticmethod
    def _boundary_from_error(
        error: AgentError | None,
    ) -> ExecutionBoundary | None:
        if error is None:
            return None
        mapping = {
            ErrorCode.MAX_STEPS_EXCEEDED: (
                BoundaryScope.STEP,
                BoundaryReason.MAX_STEPS,
            ),
            ErrorCode.STEP_TIMEOUT: (
                BoundaryScope.STEP,
                BoundaryReason.STEP_TIMEOUT,
            ),
            ErrorCode.RUN_TIMEOUT: (
                BoundaryScope.RUN,
                BoundaryReason.RUN_TIMEOUT,
            ),
        }
        boundary = mapping.get(error.code)
        if boundary is None:
            return None
        return ExecutionBoundary(
            scope=boundary[0],
            reason=boundary[1],
        )

    @staticmethod
    def _create_checkpoint(
        context: AgentContext,
        boundary: ExecutionBoundary,
    ) -> ContinuationCheckpoint:
        completed_action_ids = tuple(
            observation.action.action_id
            for observation in context.observations
        )
        evidence_ids = tuple(
            evidence.evidence_id for evidence in context.evidence
        )
        artifact_ids = tuple(
            dict.fromkeys(
                artifact.artifact_id
                for evidence in context.evidence
                for artifact in evidence.artifacts
            )
        )
        if completed_action_ids:
            progress_summary = (
                f"已完成 {len(completed_action_ids)} 个 Action，"
                f"累计 {len(evidence_ids)} 条 Evidence。"
            )
        else:
            progress_summary = (
                f"Run 在第 {context.step_index} 个推理步骤附近达到执行边界，"
                "尚无已提交 Action 结果。"
            )
        return ContinuationCheckpoint(
            checkpoint_id=str(uuid.uuid4()),
            source_run_id=context.run_id,
            objective=context.task_state.objective,
            step_index=context.step_index,
            completed_action_ids=completed_action_ids,
            evidence_ids=evidence_ids,
            artifact_ids=artifact_ids,
            progress_summary=progress_summary,
            next_step="基于已有上下文和 Evidence 继续完成原任务。",
            boundary=boundary,
        )

    @staticmethod
    def _discard_incomplete_action_batch(
        context: AgentContext,
    ) -> None:
        batch = context.current_action_batch
        if batch is None:
            return
        batch_id = batch.batch_id
        action_ids = {action.action_id for action in batch.actions}

        def references_action(message: dict[str, Any]) -> bool:
            if message.get("tool_call_id") in action_ids:
                return True
            tool_calls = message.get("tool_calls")
            return isinstance(tool_calls, list) and any(
                call.get("id") in action_ids
                for call in tool_calls
                if isinstance(call, dict)
            )

        context.messages[:] = [
            message
            for message in context.messages
            if not references_action(message)
        ]
        context.observation_batches[:] = [
            item
            for item in context.observation_batches
            if item.batch_id != batch_id
        ]
        context.observations[:] = [
            item
            for item in context.observations
            if item.action.action_id not in action_ids
        ]
        context.evidence[:] = [
            item
            for item in context.evidence
            if item.provenance.batch_id != batch_id
        ]
        context.current_action_batch = None
        context.current_batch_validation = None
        context.current_observation_batch = None
        context.current_decision = None

    @staticmethod
    def _to_result(context: AgentContext) -> AgentResult:
        if context.outcome is None:
            raise RuntimeError("Run 结束时缺少 outcome")
        return AgentResult(
            run_id=context.run_id,
            state=context.state,
            outcome=context.outcome,
            final_answer=context.final_answer,
            task_state=context.task_state,
            completion_claim=context.completion_claim,
            error=context.error,
            error_info=context.error_info,
            steps=context.step_index,
            observations=list(context.observations),
            observation_batches=list(context.observation_batches),
            evidence=list(context.evidence),
            events=list(context.events),
            termination_boundary=context.termination_boundary,
            checkpoint=context.checkpoint,
        )
