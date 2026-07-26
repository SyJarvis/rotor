from dataclasses import dataclass
import hashlib
import math
import time
from typing import Iterable, Optional

from rotor.application_settings import application_settings
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.services.loadbalancer import LoadBalancer
from rotor.gateway.adaptive import AdaptiveScorer, AdaptiveWeights


@dataclass(slots=True)
class RoutingDecision:
    candidates: list[Channel]
    strategy: str
    scores: dict[int, dict] | None = None


class RoutingEngine:
    """Select ordered channel candidates for a request."""

    def __init__(self, strategy: str = "priority_weighted"):
        self.strategy = strategy
        self._cooldowns: dict[tuple[str, int], float] = {}
        self.adaptive = AdaptiveScorer()

    def route(
        self,
        channels: Iterable[Channel],
        model: str,
        token: Token | None = None,
        request_protocol: str = "openai_chat",
        required_capabilities: Optional[set[str]] = None,
        affinity_key: str | None = None,
    ) -> RoutingDecision:
        candidates = list(channels)
        required_capabilities = required_capabilities or set()

        compatible = [
            channel
            for channel in candidates
            if channel.enabled
            and self._supports_capabilities(channel, required_capabilities)
        ]
        active = [
            channel
            for channel in compatible
            if not self._is_cooling_down(model, channel.id)
        ]
        # A cooldown should steer traffic to another compatible channel, not
        # make a model unroutable when every channel is cooling. In that case
        # retry the compatible set and let the upstream return the real error.
        candidates = active or compatible

        score_snapshot = None
        if self.strategy == "fallback_order":
            ordered = sorted(candidates, key=lambda ch: (-ch.priority, ch.id))
        elif self.strategy == "adaptive":
            affinity_ranks = None
            if affinity_key:
                affinity_ranks = {
                    channel.id: index
                    for index, channel in enumerate(
                        self._affinity_order(candidates, affinity_key)
                    )
                }
            ordered = self.adaptive.rank(
                model,
                candidates,
                tie_order=affinity_ranks,
            )
            score_snapshot = self.adaptive.snapshot(model, ordered)
        elif affinity_key:
            ordered = self._affinity_order(
                candidates,
                affinity_key,
                preserve_priority=self.strategy != "weighted",
            )
        elif self.strategy == "weighted":
            ordered = self._weighted_order(candidates)
        else:
            ordered = self._priority_weighted_order(candidates)

        return RoutingDecision(
            candidates=ordered,
            strategy=self.strategy,
            scores=score_snapshot,
        )

    def configure_adaptive(self, settings) -> None:
        self.adaptive.configure(
            weights=AdaptiveWeights(
                success=settings.adaptive_success_weight,
                latency=settings.adaptive_latency_weight,
                cost=settings.adaptive_cost_weight,
                load=settings.adaptive_load_weight,
            ),
            ewma_alpha=settings.adaptive_ewma_alpha,
            prior_successes=settings.adaptive_prior_successes,
            prior_failures=settings.adaptive_prior_failures,
            latency_target_ms=settings.adaptive_latency_target_ms,
            cost_target=settings.adaptive_cost_target,
        )

    def begin_attempt(self, model: str, channel: Channel) -> None:
        self.adaptive.begin(model, channel.id)

    def observe_result(
        self,
        model: str,
        channel: Channel,
        *,
        success: bool,
        latency_ms: int | None,
        cost: float | None = None,
    ) -> None:
        self.adaptive.observe(
            model,
            channel.id,
            success=success,
            latency_ms=latency_ms,
            cost=cost,
        )

    def mark_unavailable(
        self,
        model: str,
        channel: Channel,
        cooldown_seconds: float | None = None,
    ) -> None:
        """Temporarily suppress a channel after a retryable provider failure."""
        if cooldown_seconds is None:
            configured = (channel.extra or {}).get("fallback_cooldown_seconds", 30)
            try:
                cooldown_seconds = float(configured)
            except (TypeError, ValueError):
                cooldown_seconds = 30
        self._cooldowns[(model, channel.id)] = (
            time.monotonic() + max(cooldown_seconds, 0)
        )

    def diagnose(
        self,
        channels: Iterable[Channel],
        model: str,
        required_capabilities: Optional[set[str]] = None,
    ) -> list[dict]:
        """Return secret-free reasons why channels are or are not routable."""
        required = required_capabilities or set()
        now = time.monotonic()
        diagnostics: list[dict] = []
        for channel in channels:
            deadline = self._cooldowns.get((model, channel.id))
            cooldown_seconds = max(0.0, (deadline or 0.0) - now)
            capabilities_match = self._supports_capabilities(channel, required)
            reasons: list[str] = []
            if not channel.enabled:
                reasons.append("disabled")
            if not capabilities_match:
                reasons.append("capability_mismatch")
            if cooldown_seconds > 0:
                reasons.append("cooldown")
            adaptive_score = self.adaptive.score(model, channel)
            diagnostics.append({
                "id": channel.id,
                "name": getattr(channel, "name", f"channel-{channel.id}"),
                "protocol": channel.protocol,
                "configured_capabilities": (channel.extra or {}).get("capabilities"),
                "capabilities_match": capabilities_match,
                "cooldown_seconds": round(cooldown_seconds, 3),
                "routable": channel.enabled and capabilities_match,
                "reasons": reasons,
                "adaptive": {
                    "score": round(adaptive_score.score, 6),
                    "success_probability": round(
                        adaptive_score.success_probability, 6
                    ),
                    "latency_utility": round(
                        adaptive_score.latency_utility, 6
                    ),
                    "cost_utility": round(adaptive_score.cost_utility, 6),
                    "load_utility": round(adaptive_score.load_utility, 6),
                    "observations": adaptive_score.observations,
                },
            })
        return diagnostics

    def _is_cooling_down(self, model: str, channel_id: int) -> bool:
        key = (model, channel_id)
        deadline = self._cooldowns.get(key)
        if deadline is None:
            return False
        if deadline <= time.monotonic():
            self._cooldowns.pop(key, None)
            return False
        return True

    def _affinity_order(
        self,
        channels: list[Channel],
        affinity_key: str,
        preserve_priority: bool = True,
    ) -> list[Channel]:
        """Return a stable weighted order for an affinity key."""
        def score(channel: Channel) -> float:
            digest = hashlib.sha256(
                f"{affinity_key}:{channel.id}".encode()
            ).digest()
            uniform = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)
            weight = max(channel.weight, 0)
            if weight == 0:
                return math.inf
            return -math.log(uniform) / weight

        if preserve_priority:
            return sorted(
                channels,
                key=lambda channel: (-channel.priority, score(channel)),
            )
        return sorted(channels, key=score)

    def _supports_capabilities(self, channel: Channel, required: set[str]) -> bool:
        if not required:
            return True
        remaining = set(required)
        protocol = str(channel.protocol or "").lower()
        if "responses_native" in remaining:
            if protocol not in {"responses", "openai_responses"}:
                return False
            remaining.remove("responses_native")
        if protocol in {"responses", "openai_responses"}:
            # Declaring a native Responses channel means the upstream accepts
            # the Responses wire protocol, whose core contract includes
            # streaming and function tools. Codex always advertises tools, so
            # applying a partial optional capability allow-list here would
            # otherwise reject an otherwise valid native channel before any
            # upstream request is attempted.
            remaining.difference_update({"stream", "function_call"})
        if not remaining:
            return True
        configured = (channel.extra or {}).get("capabilities")
        if configured is None:
            return True
        capabilities = set(configured)
        return remaining.issubset(capabilities)

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


_routing_settings = application_settings.get().routing
routing_engine = RoutingEngine(strategy=_routing_settings.strategy)
routing_engine.configure_adaptive(_routing_settings)
