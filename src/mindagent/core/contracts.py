from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .context import (
    ActionRequest,
    AgentContext,
    AgentDecision,
    CompletionClaim,
    CompletionValidationResult,
    Observation,
    ObservationBatch,
    UserInputRequest,
)
from .event import AgentEvent, EventType


@dataclass
class ValidationResult:
    allowed: bool
    reason: str = ""
    require_human_approval: bool = False


@dataclass(frozen=True)
class ApprovalResult:
    approved: bool
    feedback: str | None = None
    modified_arguments: dict[str, Any] | None = None
    scope: Literal["once", "session", "always"] = "once"
    rule_key: str | None = None


class Reasoner(Protocol):
    async def decide(self, context: AgentContext) -> AgentDecision: ...


class ActionExecutor(Protocol):
    async def execute(
        self,
        context: AgentContext,
        action: ActionRequest,
    ) -> Observation: ...


class ContextManager(Protocol):
    async def build_context(self, context: AgentContext) -> None: ...

    async def update_after_observation_batch(
        self,
        context: AgentContext,
        observation_batch: ObservationBatch,
    ) -> None: ...


class PolicyEngine(Protocol):
    async def validate_action(
        self,
        context: AgentContext,
        action: ActionRequest,
    ) -> ValidationResult: ...


class CompletionGate(Protocol):
    async def validate(
        self,
        context: AgentContext,
        claim: CompletionClaim,
    ) -> CompletionValidationResult: ...


EventHandler = Callable[[AgentEvent], Awaitable[None]]
HumanApprovalHandler = Callable[
    [AgentContext, ActionRequest],
    Awaitable[bool | ApprovalResult],
]
UserInputHandler = Callable[
    [AgentContext, UserInputRequest],
    Awaitable[str],
]
EventEmitter = Callable[
    [AgentContext, EventType, dict[str, Any] | None],
    Awaitable[None],
]
