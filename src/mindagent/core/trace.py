from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .event import AgentEvent, AgentState, EventType


class TraceRecorder:
    def __init__(self, trace_dir: str | Path = "traces"):
        self.trace_dir = Path(trace_dir)
        self._lock = asyncio.Lock()

    async def __call__(self, event: AgentEvent) -> None:
        path = self.path_for(event.run_id)
        line = json.dumps(
            self.serialize(event),
            ensure_ascii=False,
            default=self._json_default,
        )
        async with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")

    def path_for(self, run_id: str) -> Path:
        return self.trace_dir / f"{run_id}.jsonl"

    @classmethod
    def replay(cls, path: str | Path) -> list[AgentEvent]:
        events = []
        with Path(path).open(encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                item = json.loads(line)
                events.append(
                    AgentEvent(
                        run_id=item["run_id"],
                        event_type=EventType(item["event_type"]),
                        state=AgentState(item["state"]),
                        payload=item.get("payload", {}),
                        event_id=item["event_id"],
                        timestamp=item["timestamp"],
                    )
                )
        return events

    @staticmethod
    def serialize(event: AgentEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "run_id": event.run_id,
            "event_type": event.event_type.value,
            "state": event.state.value,
            "timestamp": event.timestamp,
            "payload": event.payload,
        }

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, Path):
            return str(value)
        return repr(value)
