from __future__ import annotations

import asyncio
import time

from .context import AgentContext
from .contracts import EventEmitter
from .event import EventType


class Heartbeat:
    def __init__(self, interval_s: float, emit: EventEmitter):
        if interval_s <= 0:
            raise ValueError("interval_s 必须大于 0")
        self.interval_s = interval_s
        self.emit = emit

    async def run(self, context: AgentContext) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            await self.emit(
                context,
                EventType.HEARTBEAT,
                self.build_payload(context),
            )

    @staticmethod
    def build_payload(context: AgentContext) -> dict[str, object]:
        return {
            "agent_id": context.agent_id,
            "state": context.state.value,
            "step_index": context.step_index,
            "elapsed_ms": (time.time() - context.created_at) * 1000,
            "current_batch_id": (
                context.current_action_batch.batch_id
                if context.current_action_batch
                else None
            ),
            "current_action_ids": (
                [
                    action.action_id
                    for action in context.current_action_batch.actions
                ]
                if context.current_action_batch
                else []
            ),
        }
