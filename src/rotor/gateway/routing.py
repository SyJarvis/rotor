from dataclasses import dataclass
from typing import Iterable, Optional

from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.services.loadbalancer import LoadBalancer


@dataclass(slots=True)
class RoutingDecision:
    candidates: list[Channel]
    strategy: str


class RoutingEngine:
    """Select ordered channel candidates for a request."""

    def __init__(self, strategy: str = "priority_weighted"):
        self.strategy = strategy

    def route(
        self,
        channels: Iterable[Channel],
        model: str,
        token: Token | None = None,
        request_protocol: str = "openai_chat",
        required_capabilities: Optional[set[str]] = None,
    ) -> RoutingDecision:
        candidates = list(channels)
        required_capabilities = required_capabilities or set()

        candidates = [
            channel
            for channel in candidates
            if channel.enabled and self._supports_capabilities(channel, required_capabilities)
        ]

        if self.strategy == "fallback_order":
            ordered = sorted(candidates, key=lambda ch: (-ch.priority, ch.id))
        elif self.strategy == "weighted":
            ordered = self._weighted_order(candidates)
        else:
            ordered = self._priority_weighted_order(candidates)

        return RoutingDecision(candidates=ordered, strategy=self.strategy)

    def _supports_capabilities(self, channel: Channel, required: set[str]) -> bool:
        capabilities = set((channel.extra or {}).get("capabilities", []))
        if not required:
            return True
        return required.issubset(capabilities)

    def _priority_weighted_order(self, channels: list[Channel]) -> list[Channel]:
        ordered: list[Channel] = []
        remaining = channels[:]
        load_balancer = LoadBalancer(remaining)

        while len(ordered) < len(channels):
            selected = load_balancer.select_channel()
            if not selected:
                break
            ordered.append(selected)
            load_balancer.mark_failed(selected)

        return ordered

    def _weighted_order(self, channels: list[Channel]) -> list[Channel]:
        ordered: list[Channel] = []
        remaining = channels[:]

        while remaining:
            selected = LoadBalancer(remaining)._weighted_select(remaining)
            ordered.append(selected)
            remaining = [channel for channel in remaining if channel.id != selected.id]

        return ordered
