from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from .event import AgentEvent, AgentState

if TYPE_CHECKING:
    from .session import SessionEnvironment, SessionPolicy


class DecisionKind(str, Enum):
    ACTIONS = "actions"
    ASK_USER = "ask_user"
    VERIFY = "verify"
    COMPLETE = "complete"


class ActionType(str, Enum):
    TOOL = "tool"
    MODEL = "model"
    MCP = "mcp"
    SKILL = "skill"
    AGENT = "agent"
    MEMORY = "memory"


class ExecutionMode(str, Enum):
    AUTO = "auto"
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"


class ActionRisk(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    DANGEROUS = "dangerous"


class EvidenceKind(str, Enum):
    ACTION_RESULT = "action_result"
    MODEL_OUTPUT = "model_output"
    COMMAND_RESULT = "command_result"
    FILE_SNAPSHOT = "file_snapshot"
    VISUAL_FINDING = "visual_finding"
    VERIFICATION = "verification"
    ERROR = "error"


class ErrorCode(str, Enum):
    ACTION_REJECTED = "action_rejected"
    ACTION_TYPE_UNSUPPORTED = "action_type_unsupported"
    ACTION_EXECUTION_FAILED = "action_execution_failed"
    ACTION_TIMEOUT = "action_timeout"
    BATCH_TIMEOUT = "batch_timeout"
    ACTION_CANCELLED = "action_cancelled"
    HUMAN_APPROVAL_UNAVAILABLE = "human_approval_unavailable"
    HUMAN_APPROVAL_REJECTED = "human_approval_rejected"
    INVALID_DECISION = "invalid_decision"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    STEP_TIMEOUT = "step_timeout"
    RUN_TIMEOUT = "run_timeout"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class ErrorPhase(str, Enum):
    CONTEXT = "context"
    REASONING = "reasoning"
    POLICY = "policy"
    APPROVAL = "approval"
    ACTION_EXECUTION = "action_execution"
    OBSERVATION = "observation"
    CONTEXT_UPDATE = "context_update"
    RUNTIME = "runtime"


class RunOutcome(str, Enum):
    FINAL = "final"
    PARTIAL = "partial"
    ERROR = "error"
    CANCELLED = "cancelled"


class BoundaryScope(str, Enum):
    STEP = "step"
    RUN = "run"


class BoundaryReason(str, Enum):
    MAX_STEPS = "max_steps"
    STEP_TIMEOUT = "step_timeout"
    RUN_TIMEOUT = "run_timeout"


@dataclass(frozen=True)
class ExecutionBoundary:
    scope: BoundaryScope
    reason: BoundaryReason
    recoverable: bool = True
    suggested_options: tuple[str, ...] = (
        "continue",
        "adjust",
        "stop",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "reason": self.reason.value,
            "recoverable": self.recoverable,
            "suggested_options": list(self.suggested_options),
        }


@dataclass(frozen=True)
class ContinuationCheckpoint:
    checkpoint_id: str
    source_run_id: str
    objective: str
    step_index: int
    completed_action_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    progress_summary: str
    next_step: str
    boundary: ExecutionBoundary
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "source_run_id": self.source_run_id,
            "objective": self.objective,
            "step_index": self.step_index,
            "completed_action_ids": list(self.completed_action_ids),
            "evidence_ids": list(self.evidence_ids),
            "artifact_ids": list(self.artifact_ids),
            "progress_summary": self.progress_summary,
            "next_step": self.next_step,
            "boundary": self.boundary.to_dict(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class TaskConstraint:
    description: str
    constraint_id: str = field(
        default_factory=lambda: str(uuid.uuid4())
    )

    def __post_init__(self) -> None:
        if not self.constraint_id:
            raise ValueError("constraint_id 不能为空")
        if not self.description.strip():
            raise ValueError("constraint description 不能为空")


@dataclass(frozen=True)
class AcceptanceCriterion:
    description: str
    criterion_id: str = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    required: bool = True

    def __post_init__(self) -> None:
        if not self.criterion_id:
            raise ValueError("criterion_id 不能为空")
        if not self.description.strip():
            raise ValueError("criterion description 不能为空")


@dataclass
class TaskState:
    objective: str
    constraints: list[TaskConstraint] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(
        default_factory=list
    )

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("task objective 不能为空")
        self._require_unique_ids(
            [item.constraint_id for item in self.constraints],
            "constraint_id",
        )
        self._require_unique_ids(
            [
                item.criterion_id
                for item in self.acceptance_criteria
            ],
            "criterion_id",
        )

    @staticmethod
    def _require_unique_ids(values: list[str], name: str) -> None:
        if len(values) != len(set(values)):
            raise ValueError(f"TaskState 内 {name} 必须唯一")


@dataclass
class CompletionClaim:
    summary: str
    criterion_evidence: dict[str, list[str]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("completion summary 不能为空")
        for criterion_id, evidence_refs in self.criterion_evidence.items():
            if not isinstance(criterion_id, str) or not criterion_id:
                raise ValueError("criterion evidence key 不能为空")
            if not isinstance(evidence_refs, list):
                raise ValueError("criterion evidence refs 必须是 list")
            if any(
                not isinstance(ref, str) or not ref
                for ref in evidence_refs
            ):
                raise ValueError("evidence ref 不能为空")


@dataclass(frozen=True)
class CompletionValidationResult:
    accepted: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    uri: str
    media_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id 不能为空")
        if not self.uri:
            raise ValueError("artifact uri 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "media_type": self.media_type,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class Provenance:
    run_id: str
    batch_id: str
    action_id: str
    action_type: ActionType
    source_name: str
    observed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "source_name": self.source_name,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: EvidenceKind
    content: Any
    valid: bool
    provenance: Provenance
    artifacts: tuple[ArtifactRef, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ValueError("evidence_id 不能为空")

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        result = {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "valid": self.valid,
            "provenance": self.provenance.to_dict(),
            "artifacts": [
                artifact.to_dict() for artifact in self.artifacts
            ],
            "error": self.error,
        }
        if include_content:
            result["content"] = self.content
        return result


@dataclass(frozen=True)
class AgentError:
    code: ErrorCode
    message: str
    phase: ErrorPhase
    recoverable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "phase": self.phase.value,
            "recoverable": self.recoverable,
            "details": self.details,
        }


@dataclass(frozen=True)
class UserInputOption:
    value: str
    label: str
    description: str = ""

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("user input option value 不能为空")
        if not self.label.strip():
            raise ValueError("user input option label 不能为空")

    def to_dict(self) -> dict[str, str]:
        return {
            "value": self.value,
            "label": self.label,
            "description": self.description,
        }


@dataclass(frozen=True)
class UserInputRequest:
    question: str
    reason: AgentError | None = None
    action_name: str | None = None
    options: list[UserInputOption] = field(default_factory=list)
    default: str | None = None
    multiline: bool = False
    placeholder: str = ""

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("user input question 不能为空")
        values = [option.value for option in self.options]
        if len(values) != len(set(values)):
            raise ValueError("user input option value 不能重复")
        if self.default is not None and self.options:
            if self.default not in set(values):
                raise ValueError("default 必须匹配一个 option value")


@dataclass
class ActionRequest:
    action_type: ActionType
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    risk: ActionRisk = ActionRisk.READ_ONLY
    require_human_approval: bool = False
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    concurrency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, ActionType):
            raise ValueError("action_type 必须是 ActionType")
        if not self.action_id:
            raise ValueError("action_id 不能为空")


@dataclass
class ActionBatch:
    actions: list[ActionRequest]
    batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    execution_mode: ExecutionMode = ExecutionMode.AUTO

    def __post_init__(self) -> None:
        if not isinstance(self.execution_mode, ExecutionMode):
            raise ValueError("execution_mode 必须是 ExecutionMode")
        if not self.actions:
            raise ValueError("ActionBatch.actions 不能为空")
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("ActionBatch 内 action_id 必须唯一")
        if not self.batch_id:
            raise ValueError("batch_id 不能为空")


@dataclass
class AgentDecision:
    decision_kind: DecisionKind
    reasoning_summary: str = ""
    action_batch: ActionBatch | None = None
    final_answer: str | None = None
    completion_claim: CompletionClaim | None = None
    user_input_request: UserInputRequest | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.decision_kind, DecisionKind):
            raise ValueError("decision_kind 必须是 DecisionKind")
        if (
            self.decision_kind == DecisionKind.COMPLETE
            and self.completion_claim is None
        ):
            summary = (self.final_answer or "").strip()
            if summary:
                self.completion_claim = CompletionClaim(summary=summary)


@dataclass
class ActionValidation:
    action: ActionRequest
    allowed: bool
    reason: str = ""
    require_human_approval: bool = False


@dataclass
class BatchValidationResult:
    batch_id: str
    results: list[ActionValidation]

    @property
    def allowed(self) -> list[ActionRequest]:
        return [
            item.action
            for item in self.results
            if item.allowed and not item.require_human_approval
        ]

    @property
    def rejected(self) -> list[ActionValidation]:
        return [item for item in self.results if not item.allowed]

    @property
    def approval_required(self) -> list[ActionRequest]:
        return [
            item.action
            for item in self.results
            if item.allowed and item.require_human_approval
        ]


@dataclass
class Observation:
    action: ActionRequest
    ok: bool
    result: Any = None
    error: str | None = None
    error_info: AgentError | None = None
    evidence: Evidence | None = None
    elapsed_ms: float = 0.0


@dataclass
class ObservationBatch:
    batch_id: str
    observations: list[Observation]
    started_at: float = field(default_factory=time.time)
    completed_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.observations:
            raise ValueError("ObservationBatch.observations 不能为空")
        if not self.batch_id:
            raise ValueError("batch_id 不能为空")
        action_ids = [
            observation.action.action_id
            for observation in self.observations
        ]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("ObservationBatch 内 action_id 必须唯一")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at 不能早于 started_at")


@dataclass
class AgentContext:
    user_input: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str | None = None
    agent_id: str = "default_agent"
    session_environment: SessionEnvironment | None = None
    session_policy: SessionPolicy | None = None
    state: AgentState = AgentState.IDLE
    step_index: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    task_state: TaskState | None = None
    decisions: list[AgentDecision] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    current_decision: AgentDecision | None = None
    current_action_batch: ActionBatch | None = None
    current_batch_validation: BatchValidationResult | None = None
    current_observation_batch: ObservationBatch | None = None
    observation_batches: list[ObservationBatch] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    final_answer: str | None = None
    completion_claim: CompletionClaim | None = None
    outcome: RunOutcome | None = None
    termination_boundary: ExecutionBoundary | None = None
    checkpoint: ContinuationCheckpoint | None = None
    error: str | None = None
    error_info: AgentError | None = None
    pending_user_request: UserInputRequest | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancelled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.task_state is None:
            self.task_state = TaskState(objective=self.user_input)


@dataclass
class AgentResult:
    run_id: str
    state: AgentState
    outcome: RunOutcome
    final_answer: str | None
    task_state: TaskState
    completion_claim: CompletionClaim | None
    error: str | None
    error_info: AgentError | None
    steps: int
    observations: list[Observation]
    observation_batches: list[ObservationBatch]
    evidence: list[Evidence]
    events: list[AgentEvent]
    termination_boundary: ExecutionBoundary | None = None
    checkpoint: ContinuationCheckpoint | None = None


@dataclass(frozen=True)
class AgentRunStatus:
    run_id: str
    state: AgentState
    step_index: int
    final_answer: str | None
    error: str | None
    error_info: AgentError | None
    cancelled: bool
    current_batch_id: str | None
    current_action_ids: list[str]
    current_decision_kind: str | None
    pending_user_question: str | None
    created_at: float
    updated_at: float
