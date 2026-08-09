from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextScope(str, Enum):
    GLOBAL = "global"
    SESSION = "session"
    TURN = "turn"
    ITERATION = "iteration"


class ContextPressureLevel(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ContextRecord:
    kind: str
    message: dict[str, Any]
    scope: ContextScope
    required: bool = False
    priority: int = 50
    salience: float = 0.5
    group_id: str | None = None
    stable_prefix: bool = False
    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class ContextBundle:
    bundle_id: str
    records: tuple[ContextRecord, ...]
    required: bool
    priority: int
    salience: float
    stable_prefix: bool


@dataclass(frozen=True)
class ContextPressure:
    level: ContextPressureLevel
    total_tokens: int
    available_tokens: int
    ratio: float


@dataclass
class ContextPackResult:
    messages: list[dict[str, Any]]
    input_tokens: int
    available_tokens: int
    pressure: ContextPressure
    prefix_hash: str
    epoch: int
    selected_record_ids: list[str] = field(default_factory=list)
    dropped_record_ids: list[str] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
