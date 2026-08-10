from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentState(str, Enum):
    IDLE = "idle"
    CONTEXT_BUILDING = "context_building"
    THINKING = "thinking"
    ACTION_VALIDATING = "action_validating"
    NEED_HUMAN_APPROVAL = "need_human_approval"
    ACTION_EXECUTING = "action_executing"
    OBSERVING = "observing"
    CONTEXT_UPDATING = "context_updating"
    WAITING_FOR_USER = "waiting_for_user"
    FINAL = "final"
    ERROR = "error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class EventType(str, Enum):
    STATE_CHANGED = "state_changed"
    CONTEXT_PACKED = "context_packed"
    TEXT_DELTA = "text_delta"
    HEARTBEAT = "heartbeat"
    DECISION_CREATED = "decision_created"
    ACTION_VALIDATED = "action_validated"
    ACTION_STARTED = "action_started"
    ACTION_PROGRESS = "action_progress"
    ACTION_FINISHED = "action_finished"
    ACTION_CANCELLED = "action_cancelled"
    OBSERVATION_CREATED = "observation_created"
    USER_INPUT_REQUESTED = "user_input_requested"
    USER_INPUT_RECEIVED = "user_input_received"
    COMPLETION_ACCEPTED = "completion_accepted"
    COMPLETION_REJECTED = "completion_rejected"
    FINAL_CREATED = "final_created"
    ERROR_CREATED = "error_created"


@dataclass
class AgentEvent:
    run_id: str
    event_type: EventType
    state: AgentState
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class EventDeliveryFailure:
    subscriber: str
    event_id: str
    exception_type: str
    error: str
    timestamp: float = field(default_factory=time.time)
