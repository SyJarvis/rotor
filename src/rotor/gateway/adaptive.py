from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Iterable


@dataclass(frozen=True, slots=True)
class AdaptiveWeights:
    """Weights used by the first, deliberately explainable routing policy."""

    success: float = 0.55
    latency: float = 0.25
    cost: float = 0.10
    load: float = 0.10


@dataclass(slots=True)
class ChannelPerformance:
    successes: int = 0
    failures: int = 0
    latency_ewma_ms: float | None = None
    cost_ewma: float | None = None
    inflight: int = 0

    @property
    def observations(self) -> int:
        return self.successes + self.failures


@dataclass(frozen=True, slots=True)
class AdaptiveScore:
    channel_id: int
    score: float
    success_probability: float
    latency_utility: float
    cost_utility: float
    load_utility: float
    observations: int


class AdaptiveScorer:
    """Maintain online channel statistics and produce explainable scores.

    Statistics are process-local in the MVP. Every outcome is also persisted in
    Rotor's usage ledger, which provides the training source for a later model.
    """

    def __init__(
        self,
        *,
        weights: AdaptiveWeights | None = None,
        ewma_alpha: float = 0.2,
        prior_successes: float = 9.0,
        prior_failures: float = 1.0,
        latency_target_ms: float = 2_000.0,
        cost_target: float = 0.01,
    ) -> None:
        self.weights = weights or AdaptiveWeights()
        self.ewma_alpha = ewma_alpha
        self.prior_successes = prior_successes
        self.prior_failures = prior_failures
        self.latency_target_ms = latency_target_ms
        self.cost_target = cost_target
        self._stats: dict[tuple[str, int], ChannelPerformance] = {}
        self._lock = RLock()

    def configure(
        self,
        *,
        weights: AdaptiveWeights,
        ewma_alpha: float,
        prior_successes: float,
        prior_failures: float,
        latency_target_ms: float,
        cost_target: float,
    ) -> None:
        with self._lock:
            self.weights = weights
            self.ewma_alpha = ewma_alpha
            self.prior_successes = prior_successes
            self.prior_failures = prior_failures
            self.latency_target_ms = latency_target_ms
            self.cost_target = cost_target

    def begin(self, model: str, channel_id: int) -> None:
        with self._lock:
            self._get(model, channel_id).inflight += 1

    def observe(
        self,
        model: str,
        channel_id: int,
        *,
        success: bool,
        latency_ms: int | float | None,
        cost: float | None = None,
    ) -> None:
        with self._lock:
            stats = self._get(model, channel_id)
            stats.inflight = max(0, stats.inflight - 1)
            if success:
                stats.successes += 1
            else:
                stats.failures += 1
            if latency_ms is not None and latency_ms >= 0:
                stats.latency_ewma_ms = self._ewma(
                    stats.latency_ewma_ms, float(latency_ms)
                )
            if cost is not None and cost >= 0:
                stats.cost_ewma = self._ewma(stats.cost_ewma, float(cost))

    def rank(
        self,
        model: str,
        channels: Iterable[object],
        *,
        tie_order: dict[int, int] | None = None,
    ) -> list[object]:
        channels = list(channels)
        tie_order = tie_order or {}
        with self._lock:
            # Priority remains a hard administrative tier. ML only optimizes
            # within a tier so it cannot silently defeat operator intent.
            return sorted(
                channels,
                key=lambda channel: (
                    -int(getattr(channel, "priority", 0)),
                    -self.score(model, channel).score,
                    tie_order.get(
                        int(getattr(channel, "id")),
                        int(getattr(channel, "id")),
                    ),
                ),
            )

    def score(self, model: str, channel: object) -> AdaptiveScore:
        channel_id = int(getattr(channel, "id"))
        with self._lock:
            stats = self._get(model, channel_id)
            success_probability = (
                stats.successes + self.prior_successes
            ) / (
                stats.observations + self.prior_successes + self.prior_failures
            )
            latency_utility = self._inverse_utility(
                stats.latency_ewma_ms, self.latency_target_ms
            )
            cost_utility = self._inverse_utility(
                stats.cost_ewma, self.cost_target
            )
            load_utility = 1.0 / (1.0 + stats.inflight)
            cold_start_weight = max(float(getattr(channel, "weight", 1)), 0.0)
            cold_start_bonus = (
                min(cold_start_weight, 100.0) / 100_000.0
                if stats.observations == 0
                else 0.0
            )
            weights = self.weights
            score = (
                weights.success * success_probability
                + weights.latency * latency_utility
                + weights.cost * cost_utility
                + weights.load * load_utility
                + cold_start_bonus
            )
            return AdaptiveScore(
                channel_id=channel_id,
                score=score,
                success_probability=success_probability,
                latency_utility=latency_utility,
                cost_utility=cost_utility,
                load_utility=load_utility,
                observations=stats.observations,
            )

    def snapshot(self, model: str, channels: Iterable[object]) -> dict[int, dict]:
        return {
            item.channel_id: {
                "score": round(item.score, 6),
                "success_probability": round(item.success_probability, 6),
                "latency_utility": round(item.latency_utility, 6),
                "cost_utility": round(item.cost_utility, 6),
                "load_utility": round(item.load_utility, 6),
                "observations": item.observations,
            }
            for item in (self.score(model, channel) for channel in channels)
        }

    def _get(self, model: str, channel_id: int) -> ChannelPerformance:
        return self._stats.setdefault((model, channel_id), ChannelPerformance())

    def _ewma(self, previous: float | None, value: float) -> float:
        if previous is None:
            return value
        return self.ewma_alpha * value + (1.0 - self.ewma_alpha) * previous

    @staticmethod
    def _inverse_utility(value: float | None, target: float) -> float:
        if value is None:
            return 0.5
        if target <= 0:
            return 1.0 if value <= 0 else 0.0
        return target / (target + value)
