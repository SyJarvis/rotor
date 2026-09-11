from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import time
from threading import RLock
from typing import Callable, Iterable, Optional

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
    lease_channel_id: int | None = None
    lease_used: bool = False
    temporarily_unavailable: bool = False
    retry_after_seconds: int | None = None
    lease_reassessment_attempted: bool = False


@dataclass(slots=True)
class AttemptAdmission:
    engine: RoutingEngine
    model: str
    channel: Channel
    generation: int | None
    is_probe: bool
    provider_succeeded: bool = False
    observed: bool = False
    released: bool = False


@dataclass(slots=True)
class _Cooldown:
    deadline: float
    generation: int = 0
    probe: AttemptAdmission | None = None


_RESPONSES_PROTOCOLS = {"responses", "openai_responses"}
_ANTHROPIC_PROTOCOLS = {"anthropic", "anthropic_messages"}


def protocol_family(protocol: str | None) -> str:
    """Map a wire protocol to its canonical conversion family."""
    value = str(protocol or "").lower()
    if value in _RESPONSES_PROTOCOLS:
        return "responses"
    if value in _ANTHROPIC_PROTOCOLS:
        return "anthropic"
    return "openai"


class RoutingEngine:
    """Select ordered channel candidates for a request."""

    def __init__(
        self, strategy: str = "priority_weighted", *, clock: Callable[[], float] | None = None
    ):
        self.strategy = strategy
        self.protocol_affinity_enabled = True
        self._cooldowns: dict[tuple[str, int], _Cooldown] = {}
        self._clock = clock
        self._lock = RLock()
        self.adaptive = AdaptiveScorer()

    def route(
        self,
        channels: Iterable[Channel],
        model: str,
        token: Token | None = None,
        request_protocol: str = "openai_chat",
        required_capabilities: Optional[set[str]] = None,
        affinity_key: str | None = None,
        preferred_channel_id: int | None = None,
        lease_reassessment_due: bool = False,
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
        candidates = active

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

        # Protocol affinity is a soft tier, not a replacement for the
        # strategy: partition the strategy ordering into same-family and
        # convertible channels while keeping each partition's internal order.
        # Channels whose protocol family cannot represent the request at all
        # were already excluded by required_capabilities filtering above.
        if self.protocol_affinity_enabled and ordered:
            family = protocol_family(request_protocol)
            ordered = [
                *(ch for ch in ordered if protocol_family(ch.protocol) == family),
                *(ch for ch in ordered if protocol_family(ch.protocol) != family),
            ]

        lease_used = False
        lease_reassessment_attempted = bool(
            lease_reassessment_due
            and self.protocol_affinity_enabled
            and any(
                protocol_family(channel.protocol) == protocol_family(request_protocol)
                for channel in ordered
            )
        )
        if preferred_channel_id is not None and not lease_reassessment_attempted:
            leased = next(
                (
                    channel
                    for channel in ordered
                    if channel.id == preferred_channel_id
                ),
                None,
            )
            if leased is not None:
                ordered = [
                    leased,
                    *(channel for channel in ordered if channel.id != leased.id),
                ]
                lease_used = True

        return RoutingDecision(
            candidates=ordered,
            strategy=self.strategy,
            scores=score_snapshot,
            lease_channel_id=preferred_channel_id,
            lease_used=lease_used,
            temporarily_unavailable=bool(compatible and not active),
            retry_after_seconds=(
                self.retry_after_seconds(model, compatible) if compatible and not active else None
            ),
            lease_reassessment_attempted=lease_reassessment_attempted,
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
        protocol_affinity = getattr(settings, "protocol_affinity_enabled", None)
        if protocol_affinity is not None:
            self.protocol_affinity_enabled = bool(protocol_affinity)

    def begin_attempt(self, model: str, channel: Channel) -> None:
        self.adaptive.begin(model, channel.id)

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    def admit_attempt(self, model: str, channel: Channel) -> AttemptAdmission | None:
        """Atomically admit normal traffic or one process-local recovery probe."""
        with self._lock:
            if not channel.enabled:
                return None
            state = self._cooldowns.get((model, channel.id))
            if state is not None and (state.probe is not None or state.deadline > self._now()):
                return None
            admission = AttemptAdmission(
                engine=self,
                model=model,
                channel=channel,
                generation=state.generation if state is not None else None,
                is_probe=state is not None,
            )
            if state is not None:
                state.probe = admission
            self.adaptive.begin(model, channel.id)
            return admission

    def release_attempt(self, admission: AttemptAdmission) -> None:
        """Release ownership even if cancellation or persistence skipped observation."""
        with self._lock:
            if admission.released:
                return
            admission.released = True
            if not admission.observed:
                self.adaptive.end(admission.model, admission.channel.id)
            key = (admission.model, admission.channel.id)
            state = self._cooldowns.get(key)
            if state is None or state.probe is not admission:
                return
            state.probe = None
            if state.generation != admission.generation:
                return
            if admission.provider_succeeded:
                self._cooldowns.pop(key)
            else:
                # A cancelled or locally aborted probe has not proved recovery.
                self.mark_unavailable(admission.model, admission.channel)

    def retry_after_seconds(self, model: str, channels: Iterable[Channel]) -> int:
        """Return an advisory delay; a running probe has no expiry or timeout lease."""
        with self._lock:
            now = self._now()
            delays = []
            for channel in channels:
                state = self._cooldowns.get((model, channel.id))
                if state is not None:
                    delays.append(max(1.0 if state.probe is not None else 0.0, state.deadline - now))
            return max(1, math.ceil(min(delays))) if delays else 1

    def available_native_channel_ids(
        self,
        channels: Iterable[Channel],
        model: str,
        request_protocol: str,
        required_capabilities: set[str],
    ) -> set[int]:
        return {
            channel.id for channel in channels
            if channel.enabled
            and protocol_family(channel.protocol) == protocol_family(request_protocol)
            and self._supports_capabilities(channel, required_capabilities)
            and not self._is_cooling_down(model, channel.id)
        }

    def observe_result(
        self,
        model: str,
        channel: Channel,
        *,
        success: bool,
        latency_ms: int | None,
        cost: float | None = None,
        admission: AttemptAdmission | None = None,
    ) -> None:
        with self._lock:
            if admission is not None:
                if admission.observed or admission.released:
                    return
                admission.observed = True
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
        if not math.isfinite(cooldown_seconds) or cooldown_seconds < 0:
            cooldown_seconds = 30
        with self._lock:
            key = (model, channel.id)
            state = self._cooldowns.get(key)
            if state is None:
                self._cooldowns[key] = _Cooldown(self._now() + cooldown_seconds)
            else:
                state.deadline = max(state.deadline, self._now() + cooldown_seconds)
                state.generation += 1

    def diagnose(
        self,
        channels: Iterable[Channel],
        model: str,
        required_capabilities: Optional[set[str]] = None,
    ) -> list[dict]:
        """Return secret-free reasons why channels are or are not routable."""
        required = required_capabilities or set()
        now = self._now()
        diagnostics: list[dict] = []
        for channel in channels:
            with self._lock:
                state = self._cooldowns.get((model, channel.id))
                cooldown_seconds = max(0.0, state.deadline - now) if state else 0.0
                probe_busy = state is not None and state.probe is not None
            capabilities_match = self._supports_capabilities(channel, required)
            reasons: list[str] = []
            if not channel.enabled:
                reasons.append("disabled")
            if not capabilities_match:
                reasons.append("capability_mismatch")
            if cooldown_seconds > 0:
                reasons.append("cooldown")
            if probe_busy:
                reasons.append("recovery_probe_inflight")
            adaptive_score = self.adaptive.score(model, channel)
            diagnostics.append({
                "id": channel.id,
                "name": getattr(channel, "name", f"channel-{channel.id}"),
                "protocol": channel.protocol,
                "configured_capabilities": (channel.extra or {}).get("capabilities"),
                "capabilities_match": capabilities_match,
                "cooldown_seconds": round(cooldown_seconds, 3),
                "routable": channel.enabled and capabilities_match and cooldown_seconds == 0 and not probe_busy,
                "recovery_probe_eligible": channel.enabled and capabilities_match and state is not None and cooldown_seconds == 0 and not probe_busy,
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
        with self._lock:
            state = self._cooldowns.get((model, channel_id))
            return state is not None and (state.probe is not None or state.deadline > self._now())

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
        provider_type = str(getattr(channel, "type", "") or "").lower()
        chat_protocol = (
            protocol not in _RESPONSES_PROTOCOLS | _ANTHROPIC_PROTOCOLS
            and provider_type != "anthropic"
        )
        if "stop_sequences" in remaining:
            if protocol in _RESPONSES_PROTOCOLS:
                return False
            remaining.remove("stop_sequences")
        if "openai_chat_native" in remaining:
            if not chat_protocol:
                return False
            remaining.remove("openai_chat_native")
        if "anthropic_native" in remaining:
            # Zhipu's Chat adapter forwards Anthropic payloads through its
            # native Messages endpoint when an Anthropic request is present.
            native_anthropic = protocol in _ANTHROPIC_PROTOCOLS or (
                protocol not in _RESPONSES_PROTOCOLS
                and provider_type in {"anthropic", "zhipu"}
            )
            if not native_anthropic:
                return False
            remaining.remove("anthropic_native")
        if "reasoning_effort" in remaining:
            if not chat_protocol and protocol not in _RESPONSES_PROTOCOLS:
                return False
            remaining.remove("reasoning_effort")
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


def session_lease_success_reason(
    decision: RoutingDecision | None,
    attempt_index: int,
    *,
    channel: Channel | None = None,
    request_protocol: str | None = None,
) -> str:
    if decision is not None and getattr(decision, "lease_reassessment_attempted", False) and channel is not None:
        if protocol_family(channel.protocol) == protocol_family(request_protocol):
            return "protocol_recovered"
        if channel.id == decision.lease_channel_id:
            return "protocol_reassessment_deferred"
        return "fallback_success"
    if attempt_index > 0:
        return "fallback_success"
    if (
        decision is not None
        and getattr(decision, "lease_channel_id", None) is not None
        and not getattr(decision, "lease_used", False)
    ):
        return "leased_channel_unavailable"
    return "request_success"


_routing_settings = application_settings.get().routing
routing_engine = RoutingEngine(strategy=_routing_settings.strategy)
routing_engine.configure_adaptive(_routing_settings)
