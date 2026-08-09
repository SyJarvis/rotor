from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from .context import (
    ActionBatch,
    ActionRequest,
    ActionType,
    ActionValidation,
    AgentError,
    AgentContext,
    AgentDecision,
    ArtifactRef,
    BatchValidationResult,
    DecisionKind,
    ErrorCode,
    ErrorPhase,
    Evidence,
    EvidenceKind,
    Observation,
    ObservationBatch,
    Provenance,
    UserInputRequest,
)
from .contracts import (
    ApprovalResult,
    ContextManager,
    CompletionGate,
    EventEmitter,
    HumanApprovalHandler,
    PolicyEngine,
    Reasoner,
    UserInputHandler,
)
from .event import AgentState, EventType
from .scheduler import ActionScheduler
from .state_machine import AgentStateMachine


T = TypeVar("T")


class StepTimeoutError(TimeoutError):
    def __init__(self, phase: ErrorPhase):
        self.phase = phase
        super().__init__(f"{phase.value} step 超过超时时间")


class ReActLoop:
    terminal_states = {
        AgentState.FINAL,
        AgentState.ERROR,
        AgentState.CANCELLED,
        AgentState.TIMEOUT,
    }

    def __init__(
        self,
        *,
        reasoner: Reasoner,
        scheduler: ActionScheduler,
        context_manager: ContextManager,
        policy_engine: PolicyEngine,
        completion_gate: CompletionGate,
        state_machine: AgentStateMachine,
        emit: EventEmitter,
        max_steps: int,
        step_timeout_s: float | None,
        human_approval_handler: HumanApprovalHandler | None = None,
        user_input_handler: UserInputHandler | None = None,
    ):
        self.reasoner = reasoner
        self.scheduler = scheduler
        self.context_manager = context_manager
        self.policy_engine = policy_engine
        self.completion_gate = completion_gate
        self.state_machine = state_machine
        self.emit = emit
        self.max_steps = max_steps
        self.step_timeout_s = step_timeout_s
        self.human_approval_handler = human_approval_handler
        self.user_input_handler = user_input_handler

    async def run(self, context: AgentContext) -> None:
        await self._set_state(context, AgentState.CONTEXT_BUILDING)
        await self._run_step(
            self.context_manager.build_context(context),
            ErrorPhase.CONTEXT,
        )
        await self._set_state(context, AgentState.THINKING)

        while context.state not in self.terminal_states:
            if context.cancelled:
                await self._set_state(context, AgentState.CANCELLED)
            elif self._should_interrupt(context):
                await self._interrupt(context)
            elif context.state == AgentState.THINKING:
                await self._handle_thinking(context)
            elif context.state == AgentState.ACTION_VALIDATING:
                await self._validate_action(context)
            elif context.state == AgentState.NEED_HUMAN_APPROVAL:
                await self._request_human_approval(context)
            elif context.state == AgentState.ACTION_EXECUTING:
                await self._execute_action(context)
            elif context.state == AgentState.OBSERVING:
                await self._observe(context)
            elif context.state == AgentState.CONTEXT_UPDATING:
                await self._update_context(context)
            elif context.state == AgentState.WAITING_FOR_USER:
                await self._wait_for_user(context)
            else:
                await self._fail(
                    context,
                    f"ReActLoop 无法处理状态: {context.state.value}",
                )

    async def _handle_thinking(self, context: AgentContext) -> None:
        if context.step_index >= self.max_steps:
            await self._fail(
                context,
                "超过最大 ReAct 步数，可能陷入循环",
                AgentError(
                    code=ErrorCode.MAX_STEPS_EXCEEDED,
                    message="超过最大 ReAct 步数，可能陷入循环",
                    phase=ErrorPhase.REASONING,
                    recoverable=True,
                ),
            )
            return

        prepare_context = getattr(
            self.context_manager,
            "prepare_context",
            None,
        )
        if callable(prepare_context):
            tools = list(getattr(self.reasoner, "tools", ()))
            pack_result = await self._run_step(
                prepare_context(context, tools=tools),
                ErrorPhase.CONTEXT,
            )
            if pack_result is not None:
                await self.emit(
                    context,
                    EventType.CONTEXT_PACKED,
                    {
                        "epoch": pack_result.epoch,
                        "prefix_hash": pack_result.prefix_hash,
                        "input_tokens": pack_result.input_tokens,
                        "available_tokens": pack_result.available_tokens,
                        "pressure": pack_result.pressure.level.value,
                        "pressure_ratio": pack_result.pressure.ratio,
                        "dropped_records": len(
                            pack_result.dropped_record_ids
                        ),
                    },
                )

        decision = await self._run_step(
            self.reasoner.decide(context),
            ErrorPhase.REASONING,
        )
        if self._interrupt_requested(context):
            await self._interrupt(context)
            return
        if not isinstance(decision, AgentDecision):
            await self._fail(
                context,
                "Reasoner.decide 必须返回 AgentDecision",
                AgentError(
                    code=ErrorCode.INVALID_DECISION,
                    message="Reasoner.decide 必须返回 AgentDecision",
                    phase=ErrorPhase.REASONING,
                ),
            )
            return

        context.step_index += 1
        context.current_decision = decision
        context.decisions.append(decision)
        record_decision = getattr(
            self.context_manager,
            "record_decision",
            None,
        )
        if callable(record_decision):
            await self._run_step(
                record_decision(context, decision),
                ErrorPhase.CONTEXT_UPDATE,
            )
        await self.emit(
            context,
            EventType.DECISION_CREATED,
            {
                "decision_kind": decision.decision_kind.value,
                "reasoning_summary": decision.reasoning_summary,
                "action_names": (
                    [
                        action.name
                        for action in decision.action_batch.actions
                    ]
                    if decision.action_batch
                    else []
                ),
                "batch_id": (
                    decision.action_batch.batch_id
                    if decision.action_batch
                    else None
                ),
                "action_ids": (
                    [
                        action.action_id
                        for action in decision.action_batch.actions
                    ]
                    if decision.action_batch
                    else []
                ),
            },
        )

        if decision.decision_kind == DecisionKind.COMPLETE:
            if decision.action_batch is not None:
                await self._fail(
                    context,
                    "完成决策不能包含 action_batch",
                    AgentError(
                        code=ErrorCode.INVALID_DECISION,
                        message="完成决策不能包含 action_batch",
                        phase=ErrorPhase.REASONING,
                    ),
                )
                return
            claim = decision.completion_claim
            if claim is None:
                await self._fail(
                    context,
                    "完成决策必须包含 completion_claim",
                    AgentError(
                        code=ErrorCode.INVALID_DECISION,
                        message=(
                            "完成决策必须包含 completion_claim"
                        ),
                        phase=ErrorPhase.REASONING,
                    ),
                )
                return
            validation = await self._run_step(
                self.completion_gate.validate(context, claim),
                ErrorPhase.REASONING,
            )
            if not validation.accepted:
                record_feedback = getattr(
                    self.context_manager,
                    "record_completion_feedback",
                    None,
                )
                if callable(record_feedback):
                    await self._run_step(
                        record_feedback(context, validation.reasons),
                        ErrorPhase.CONTEXT_UPDATE,
                    )
                await self.emit(
                    context,
                    EventType.COMPLETION_REJECTED,
                    {"reasons": list(validation.reasons)},
                )
                context.current_decision = None
                return
            context.final_answer = (
                decision.final_answer or claim.summary
            )
            context.completion_claim = decision.completion_claim
            await self.emit(
                context,
                EventType.COMPLETION_ACCEPTED,
                {"summary": claim.summary},
            )
            await self._set_state(context, AgentState.FINAL)
            await self.emit(
                context,
                EventType.FINAL_CREATED,
                {"final_answer": context.final_answer},
            )
            return

        if decision.decision_kind == DecisionKind.ASK_USER:
            if decision.action_batch is not None:
                await self._fail(
                    context,
                    "ASK_USER 决策不能包含 action_batch",
                    AgentError(
                        code=ErrorCode.INVALID_DECISION,
                        message="ASK_USER 决策不能包含 action_batch",
                        phase=ErrorPhase.REASONING,
                    ),
                )
                return
            if decision.user_input_request is None:
                await self._fail(
                    context,
                    "ASK_USER 决策必须包含 user_input_request",
                    AgentError(
                        code=ErrorCode.INVALID_DECISION,
                        message=(
                            "ASK_USER 决策必须包含 "
                            "user_input_request"
                        ),
                        phase=ErrorPhase.REASONING,
                    ),
                )
                return
            context.pending_user_request = decision.user_input_request
            await self._set_state(context, AgentState.WAITING_FOR_USER)
            return

        if decision.decision_kind not in {
            DecisionKind.ACTIONS,
            DecisionKind.VERIFY,
        }:
            await self._fail(
                context,
                f"当前 ReActLoop 不支持决策: {decision.decision_kind.value}",
                AgentError(
                    code=ErrorCode.INVALID_DECISION,
                    message=(
                        "当前 ReActLoop 不支持决策: "
                        f"{decision.decision_kind.value}"
                    ),
                    phase=ErrorPhase.REASONING,
                ),
            )
            return

        action_batch = decision.action_batch
        if action_batch is None:
            await self._fail(
                context,
                "ACTIONS 决策必须包含 action_batch",
                AgentError(
                    code=ErrorCode.INVALID_DECISION,
                    message="ACTIONS 决策必须包含 action_batch",
                    phase=ErrorPhase.REASONING,
                ),
            )
            return
        context.current_action_batch = action_batch
        await self._set_state(context, AgentState.ACTION_VALIDATING)

    async def _validate_action(self, context: AgentContext) -> None:
        batch = self._require_batch(context, "校验")
        results: list[ActionValidation] = []
        for action in batch.actions:
            validation = await self._run_step(
                self.policy_engine.validate_action(context, action),
                ErrorPhase.POLICY,
            )
            results.append(
                ActionValidation(
                    action=action,
                    allowed=validation.allowed,
                    reason=validation.reason,
                    require_human_approval=(
                        validation.require_human_approval
                    ),
                )
            )
            await self.emit(
                context,
                EventType.ACTION_VALIDATED,
                {
                    "batch_id": batch.batch_id,
                    "action_id": action.action_id,
                    "action_name": action.name,
                    "allowed": validation.allowed,
                    "reason": validation.reason,
                    "require_human_approval": (
                        validation.require_human_approval
                    ),
                },
            )

        batch_validation = BatchValidationResult(
            batch_id=batch.batch_id,
            results=results,
        )
        context.current_batch_validation = batch_validation

        if batch_validation.approval_required:
            await self._set_state(context, AgentState.NEED_HUMAN_APPROVAL)
        elif batch_validation.allowed:
            await self._set_state(context, AgentState.ACTION_EXECUTING)
        else:
            self._set_rejected_observations(context)
            await self._set_state(context, AgentState.OBSERVING)

    async def _request_human_approval(self, context: AgentContext) -> None:
        validation = self._require_validation(context)
        if self.human_approval_handler is None:
            action_names = ", ".join(
                action.name
                for action in validation.approval_required
            )
            message = (
                f"Action {action_names} 需要人工确认，"
                "但未提供 human_approval_handler"
            )
            await self._fail(
                context,
                message,
                AgentError(
                    code=ErrorCode.HUMAN_APPROVAL_UNAVAILABLE,
                    message=message,
                    phase=ErrorPhase.APPROVAL,
                    details={"action_names": action_names},
                ),
            )
            return

        for item in validation.results:
            if not item.allowed or not item.require_human_approval:
                continue
            cached = self._approval_from_context(context, item.action)
            if cached is not None:
                item.action.arguments = cached.get(
                    "modified_arguments",
                    item.action.arguments,
                )
                item.require_human_approval = False
                continue
            approved = await self._run_step(
                self.human_approval_handler(context, item.action),
                ErrorPhase.APPROVAL,
            )
            approval = self._normalize_approval_result(approved)
            if approval.approved:
                if approval.modified_arguments is not None:
                    item.action.arguments = approval.modified_arguments
                self._record_approval_rule(context, item.action, approval)
                item.require_human_approval = False
            else:
                item.allowed = False
                item.reason = self._approval_rejection_reason(approval)
                item.require_human_approval = False

        if validation.allowed:
            await self._set_state(context, AgentState.ACTION_EXECUTING)
        else:
            self._set_rejected_observations(context)
            await self._set_state(context, AgentState.OBSERVING)

    async def _execute_action(self, context: AgentContext) -> None:
        original_batch = self._require_batch(context, "执行")
        validation = self._require_validation(context)
        allowed_ids = {
            action.action_id
            for action in validation.allowed
        }
        executable_batch = ActionBatch(
            actions=[
                action
                for action in original_batch.actions
                if action.action_id in allowed_ids
            ],
            batch_id=original_batch.batch_id,
            execution_mode=original_batch.execution_mode,
        )
        executed = await self._run_step(
            self.scheduler.execute(
                context,
                executable_batch,
                on_action_started=lambda action: self._emit_action_started(
                    context,
                    original_batch,
                    action,
                ),
                on_action_finished=lambda action, observation: (
                    self._emit_action_finished(
                        context,
                        original_batch,
                        action,
                        observation,
                    )
                ),
                on_action_cancelled=lambda action, observation: (
                    self._emit_action_cancelled(
                        context,
                        original_batch,
                        action,
                        observation,
                    )
                ),
            ),
            ErrorPhase.ACTION_EXECUTION,
        )
        context.current_observation_batch = self._merge_observations(
            original_batch,
            executed,
            validation,
        )
        await self._set_state(context, AgentState.OBSERVING)

    async def _observe(self, context: AgentContext) -> None:
        observation_batch = context.current_observation_batch
        if observation_batch is None:
            raise RuntimeError("没有 current_observation_batch 可观察")
        context.observation_batches.append(observation_batch)
        for observation in observation_batch.observations:
            observation.evidence = self._evidence_from_observation(
                context,
                observation_batch.batch_id,
                observation,
            )
            if any(
                item.evidence_id == observation.evidence.evidence_id
                for item in context.evidence
            ):
                raise ValueError(
                    "Evidence ID 在当前 run 内重复: "
                    f"{observation.evidence.evidence_id}"
                )
            context.observations.append(observation)
            context.evidence.append(observation.evidence)
            await self.emit(
                context,
                EventType.OBSERVATION_CREATED,
                {
                    "batch_id": observation_batch.batch_id,
                    "action_id": observation.action.action_id,
                    "action_name": observation.action.name,
                    "ok": observation.ok,
                    "result": observation.result,
                    "error": observation.error,
                    "error_info": (
                        observation.error_info.to_dict()
                        if observation.error_info
                        else None
                    ),
                    "evidence": observation.evidence.to_dict(),
                },
            )
        await self._set_state(context, AgentState.CONTEXT_UPDATING)

    @staticmethod
    def _evidence_from_observation(
        context: AgentContext,
        batch_id: str,
        observation: Observation,
    ) -> Evidence:
        action = observation.action
        result = observation.result
        return Evidence(
            evidence_id=f"evidence:{batch_id}:{action.action_id}",
            kind=ReActLoop._evidence_kind(action.name, observation),
            content=result,
            valid=ReActLoop._evidence_is_valid(action.name, observation),
            provenance=Provenance(
                run_id=context.run_id,
                batch_id=batch_id,
                action_id=action.action_id,
                action_type=action.action_type,
                source_name=action.name,
            ),
            artifacts=ReActLoop._artifact_refs(observation.result),
            error=observation.error,
        )

    @staticmethod
    def _evidence_is_valid(
        action_name: str,
        observation: Observation,
    ) -> bool:
        if not observation.ok:
            return False
        if action_name == "model_verify":
            return (
                isinstance(observation.result, dict)
                and observation.result.get("verified") is True
            )
        if action_name != "exec_command":
            return True
        result = observation.result
        return (
            isinstance(result, dict)
            and result.get("exit_code") == 0
            and result.get("timed_out") is False
        )

    @staticmethod
    def _evidence_kind(
        action_name: str,
        observation: Observation,
    ) -> EvidenceKind:
        if not observation.ok:
            return EvidenceKind.ERROR
        if action_name == "exec_command":
            return EvidenceKind.COMMAND_RESULT
        if action_name in {"file_read", "file_edit"}:
            return EvidenceKind.FILE_SNAPSHOT
        if action_name == "image_understanding":
            return EvidenceKind.VISUAL_FINDING
        if action_name == "model_inspect_image":
            return EvidenceKind.VISUAL_FINDING
        if action_name == "model_verify":
            return EvidenceKind.VERIFICATION
        if observation.action.action_type == ActionType.MODEL:
            return EvidenceKind.MODEL_OUTPUT
        return EvidenceKind.ACTION_RESULT

    @staticmethod
    def _artifact_refs(result: object) -> tuple[ArtifactRef, ...]:
        if not isinstance(result, dict):
            return ()
        raw_refs = result.get("artifact_refs")
        if not isinstance(raw_refs, list):
            return ()
        refs = []
        for raw in raw_refs:
            if not isinstance(raw, dict):
                continue
            try:
                refs.append(
                    ArtifactRef(
                        artifact_id=raw["artifact_id"],
                        uri=raw["uri"],
                        media_type=raw.get("media_type"),
                        metadata=raw.get("metadata", {}),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(refs)

    async def _update_context(self, context: AgentContext) -> None:
        observation_batch = context.current_observation_batch
        if observation_batch is None:
            raise RuntimeError("没有 observation batch 可更新上下文")
        await self._run_step(
            self.context_manager.update_after_observation_batch(
                context,
                observation_batch,
            ),
            ErrorPhase.CONTEXT_UPDATE,
        )
        context.current_action_batch = None
        context.current_batch_validation = None
        context.current_observation_batch = None
        context.current_decision = None
        if context.pending_user_request is not None:
            await self._set_state(context, AgentState.WAITING_FOR_USER)
        else:
            await self._set_state(context, AgentState.THINKING)

    async def _wait_for_user(self, context: AgentContext) -> None:
        request = context.pending_user_request
        if request is None:
            raise RuntimeError("没有 pending_user_request 可等待")
        if self.user_input_handler is None:
            await self._fail(
                context,
                "Agent 正在等待用户输入，但未配置 user_input_handler",
                AgentError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=(
                        "Agent 正在等待用户输入，"
                        "但未配置 user_input_handler"
                    ),
                    phase=ErrorPhase.RUNTIME,
                ),
            )
            return

        await self.emit(
            context,
            EventType.USER_INPUT_REQUESTED,
            {
                "question": request.question,
                "action_name": request.action_name,
                "reason": (
                    request.reason.to_dict()
                    if request.reason is not None
                    else None
                ),
                "options": [
                    option.to_dict() for option in request.options
                ],
                "default": request.default,
                "multiline": request.multiline,
                "placeholder": request.placeholder,
            },
        )
        user_input = await self.user_input_handler(context, request)
        context.metadata.pop("_user_response_submitted", None)
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("用户回复必须是非空字符串")

        record_user_input = getattr(
            self.context_manager,
            "record_user_input",
            None,
        )
        if callable(record_user_input):
            await self._run_step(
                record_user_input(context, user_input),
                ErrorPhase.CONTEXT_UPDATE,
            )
        else:
            context.messages.append(
                {"role": "user", "content": user_input}
            )
        await self.emit(
            context,
            EventType.USER_INPUT_RECEIVED,
            {"response": user_input},
        )
        context.pending_user_request = None
        await self._set_state(context, AgentState.THINKING)

    async def _set_state(
        self,
        context: AgentContext,
        new_state: AgentState,
    ) -> None:
        old_state = context.state
        self.state_machine.transition(context, new_state)
        await self.emit(
            context,
            EventType.STATE_CHANGED,
            {"old_state": old_state.value, "new_state": new_state.value},
        )

    async def _fail(
        self,
        context: AgentContext,
        error: str,
        error_info: AgentError | None = None,
    ) -> None:
        context.error = error
        context.error_info = error_info or AgentError(
            code=ErrorCode.INTERNAL_ERROR,
            message=error,
            phase=ErrorPhase.RUNTIME,
        )
        await self._set_state(context, AgentState.ERROR)
        await self.emit(
            context,
            EventType.ERROR_CREATED,
            {
                "error": error,
                "error_info": context.error_info.to_dict(),
            },
        )

    async def _run_step(
        self,
        awaitable: Awaitable[T],
        phase: ErrorPhase,
    ) -> T:
        if self.step_timeout_s is None:
            return await awaitable
        try:
            return await asyncio.wait_for(
                awaitable,
                timeout=self.step_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise StepTimeoutError(phase) from exc

    @staticmethod
    def _require_batch(
        context: AgentContext,
        operation: str,
    ) -> ActionBatch:
        action_batch = context.current_action_batch
        if action_batch is None:
            raise RuntimeError(f"没有 ActionBatch 可{operation}")
        return action_batch

    @staticmethod
    def _require_validation(
        context: AgentContext,
    ) -> BatchValidationResult:
        if context.current_batch_validation is None:
            raise RuntimeError("没有 BatchValidationResult")
        return context.current_batch_validation

    @staticmethod
    def _should_interrupt(context: AgentContext) -> bool:
        return (
            ReActLoop._interrupt_requested(context)
            and context.state == AgentState.THINKING
        )

    @staticmethod
    def _interrupt_requested(context: AgentContext) -> bool:
        return bool(context.metadata.get("_interrupt_requested"))

    async def _interrupt(self, context: AgentContext) -> None:
        reason = (
            context.metadata.get("_interrupt_reason")
            or "Agent run 已被用户中断"
        )
        context.cancelled = True
        context.error = str(reason)
        context.error_info = AgentError(
            code=ErrorCode.CANCELLED,
            message=context.error,
            phase=ErrorPhase.RUNTIME,
            recoverable=True,
        )
        await self._set_state(context, AgentState.CANCELLED)

    @staticmethod
    def _normalize_approval_result(
        result: bool | ApprovalResult,
    ) -> ApprovalResult:
        if isinstance(result, ApprovalResult):
            return result
        return ApprovalResult(approved=bool(result))

    @staticmethod
    def _approval_rule_key(
        action: ActionRequest,
        approval: ApprovalResult | None = None,
    ) -> str:
        if approval is not None and approval.rule_key:
            return approval.rule_key
        return action.metadata.get("approval_rule_key") or action.name

    @classmethod
    def _approval_from_context(
        cls,
        context: AgentContext,
        action: ActionRequest,
    ) -> dict[str, Any] | None:
        rules = context.metadata.get("_approval_rules")
        if not isinstance(rules, dict):
            return None
        rule = rules.get(cls._approval_rule_key(action))
        return rule if isinstance(rule, dict) else None

    @classmethod
    def _record_approval_rule(
        cls,
        context: AgentContext,
        action: ActionRequest,
        approval: ApprovalResult,
    ) -> None:
        if approval.scope == "once":
            return
        rules = context.metadata.setdefault("_approval_rules", {})
        if not isinstance(rules, dict):
            rules = {}
            context.metadata["_approval_rules"] = rules
        rule: dict[str, Any] = {"approved": True}
        if approval.modified_arguments is not None:
            rule["modified_arguments"] = approval.modified_arguments
        rules[cls._approval_rule_key(action, approval)] = rule

    @staticmethod
    def _approval_rejection_reason(result: ApprovalResult) -> str:
        feedback = (result.feedback or "").strip()
        if feedback:
            return f"人工拒绝执行：{feedback}"
        return "人工拒绝执行"

    @staticmethod
    def _rejection_error(item: ActionValidation) -> AgentError:
        approval_rejected = item.reason.startswith("人工拒绝执行")
        message = (
            f"人工拒绝执行 action: {item.action.name}"
            if approval_rejected
            else f"Action 被策略拒绝: {item.reason}"
        )
        return AgentError(
            code=(
                ErrorCode.HUMAN_APPROVAL_REJECTED
                if approval_rejected
                else ErrorCode.ACTION_REJECTED
            ),
            message=message,
            phase=(
                ErrorPhase.APPROVAL
                if approval_rejected
                else ErrorPhase.POLICY
            ),
            recoverable=True,
            details={
                "action_id": item.action.action_id,
                "action_name": item.action.name,
                "reason": item.reason,
            },
        )

    @classmethod
    def _rejected_observation(
        cls,
        item: ActionValidation,
    ) -> Observation:
        error = cls._rejection_error(item)
        return Observation(
            action=item.action,
            ok=False,
            error=error.message,
            error_info=error,
        )

    @classmethod
    def _set_rejected_observations(
        cls,
        context: AgentContext,
    ) -> None:
        batch = cls._require_batch(context, "生成拒绝结果")
        validation = cls._require_validation(context)
        context.current_observation_batch = ObservationBatch(
            batch_id=batch.batch_id,
            observations=[
                cls._rejected_observation(item)
                for item in validation.rejected
            ],
        )

    @classmethod
    def _merge_observations(
        cls,
        original_batch: ActionBatch,
        executed: ObservationBatch,
        validation: BatchValidationResult,
    ) -> ObservationBatch:
        by_action_id = {
            observation.action.action_id: observation
            for observation in executed.observations
        }
        by_action_id.update(
            {
                item.action.action_id: cls._rejected_observation(item)
                for item in validation.rejected
            }
        )
        return ObservationBatch(
            batch_id=original_batch.batch_id,
            observations=[
                by_action_id[action.action_id]
                for action in original_batch.actions
            ],
            started_at=executed.started_at,
            completed_at=executed.completed_at,
        )

    async def _emit_action_started(
        self,
        context: AgentContext,
        batch: ActionBatch,
        action: ActionRequest,
    ) -> None:
        await self.emit(
            context,
            EventType.ACTION_STARTED,
            {
                "batch_id": batch.batch_id,
                "action_id": action.action_id,
                "action_type": action.action_type.value,
                "action_name": action.name,
                "arguments": action.arguments,
                "risk": action.risk.value,
            },
        )

    async def _emit_action_finished(
        self,
        context: AgentContext,
        batch: ActionBatch,
        action: ActionRequest,
        observation: Observation,
    ) -> None:
        await self.emit(
            context,
            EventType.ACTION_FINISHED,
            {
                "batch_id": batch.batch_id,
                "action_id": action.action_id,
                "action_name": action.name,
                "ok": observation.ok,
                "elapsed_ms": observation.elapsed_ms,
                "error": observation.error,
                "error_info": (
                    observation.error_info.to_dict()
                    if observation.error_info
                    else None
                ),
            },
        )

    async def _emit_action_cancelled(
        self,
        context: AgentContext,
        batch: ActionBatch,
        action: ActionRequest,
        observation: Observation,
    ) -> None:
        await self.emit(
            context,
            EventType.ACTION_CANCELLED,
            {
                "batch_id": batch.batch_id,
                "action_id": action.action_id,
                "action_name": action.name,
                "elapsed_ms": observation.elapsed_ms,
                "error": observation.error,
                "error_info": (
                    observation.error_info.to_dict()
                    if observation.error_info
                    else None
                ),
            },
        )
