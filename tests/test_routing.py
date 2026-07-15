from types import SimpleNamespace

from rotor.gateway.routing import RoutingEngine


def _channel(channel_id: int, priority: int = 10, weight: int = 1):
    return SimpleNamespace(
        id=channel_id,
        priority=priority,
        weight=weight,
        enabled=True,
        extra={},
    )


def test_affinity_keeps_same_conversation_on_same_channel() -> None:
    engine = RoutingEngine()
    channels = [_channel(1), _channel(2), _channel(3)]

    first = engine.route(channels, "model", affinity_key="session-a")
    second = engine.route(list(reversed(channels)), "model", affinity_key="session-a")

    assert [channel.id for channel in first.candidates] == [
        channel.id for channel in second.candidates
    ]


def test_affinity_preserves_priority_before_weighted_score() -> None:
    engine = RoutingEngine()
    channels = [_channel(1, priority=1), _channel(2, priority=10)]

    decision = engine.route(channels, "model", affinity_key="session-a")

    assert decision.candidates[0].id == 2


def test_retryable_failure_temporarily_removes_channel() -> None:
    engine = RoutingEngine()
    channels = [_channel(1, priority=10), _channel(2, priority=1)]

    engine.mark_unavailable("model", channels[0], cooldown_seconds=60)
    decision = engine.route(channels, "model", affinity_key="session-a")

    assert [channel.id for channel in decision.candidates] == [2]


def test_zero_cooldown_immediately_restores_channel() -> None:
    engine = RoutingEngine()
    channels = [_channel(1, priority=10), _channel(2, priority=1)]

    engine.mark_unavailable("model", channels[0], cooldown_seconds=0)
    decision = engine.route(channels, "model", affinity_key="session-a")

    assert decision.candidates[0].id == 1


def test_fallback_order_is_priority_then_oldest_id() -> None:
    engine = RoutingEngine(strategy="fallback_order")
    channels = [
        _channel(3, priority=1),
        _channel(2, priority=10),
        _channel(1, priority=10),
    ]

    decision = engine.route(channels, "model")

    assert [channel.id for channel in decision.candidates] == [1, 2, 3]


def test_fallback_order_ignores_affinity() -> None:
    engine = RoutingEngine(strategy="fallback_order")
    channels = [_channel(2), _channel(1)]

    decision = engine.route(channels, "model", affinity_key="session-a")

    assert [channel.id for channel in decision.candidates] == [1, 2]
