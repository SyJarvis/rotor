from __future__ import annotations

import time

from .context import AgentContext
from .event import AgentState


class StateMachineError(RuntimeError):
    pass


class AgentStateMachine:
    allowed_transitions = {
        AgentState.IDLE: {AgentState.CONTEXT_BUILDING, AgentState.CANCELLED},
        AgentState.CONTEXT_BUILDING: {
            AgentState.THINKING,
            AgentState.ERROR,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        },
        AgentState.THINKING: {
            AgentState.ACTION_VALIDATING,
            AgentState.WAITING_FOR_USER,
            AgentState.FINAL,
            AgentState.ERROR,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        },
        AgentState.ACTION_VALIDATING: {
            AgentState.NEED_HUMAN_APPROVAL,
            AgentState.ACTION_EXECUTING,
            AgentState.OBSERVING,
            AgentState.ERROR,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        },
        AgentState.NEED_HUMAN_APPROVAL: {
            AgentState.ACTION_EXECUTING,
            AgentState.OBSERVING,
            AgentState.ERROR,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        },
        AgentState.ACTION_EXECUTING: {
            AgentState.OBSERVING,
            AgentState.ERROR,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        },
        AgentState.OBSERVING: {
            AgentState.CONTEXT_UPDATING,
            AgentState.ERROR,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        },
        AgentState.CONTEXT_UPDATING: {
            AgentState.THINKING,
            AgentState.WAITING_FOR_USER,
            AgentState.ERROR,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        },
        AgentState.WAITING_FOR_USER: {
            AgentState.THINKING,
            AgentState.ERROR,
            AgentState.CANCELLED,
            AgentState.TIMEOUT,
        },
        AgentState.FINAL: set(),
        AgentState.ERROR: set(),
        AgentState.CANCELLED: set(),
        AgentState.TIMEOUT: set(),
    }

    def can_transition(
        self,
        old_state: AgentState,
        new_state: AgentState,
    ) -> bool:
        return new_state in self.allowed_transitions.get(old_state, set())

    def transition(
        self,
        context: AgentContext,
        new_state: AgentState,
    ) -> None:
        old_state = context.state
        if not self.can_transition(old_state, new_state):
            raise StateMachineError(
                f"非法状态流转: {old_state.value} -> {new_state.value}"
            )
        context.state = new_state
        context.updated_at = time.time()
