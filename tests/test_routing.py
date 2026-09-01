from types import SimpleNamespace

from rotor.gateway.routing import RoutingEngine


def _channel(
    channel_id: int,
    priority: int = 10,
    weight: int = 1,
    protocol: str = "openai",
):
    return SimpleNamespace(
        id=channel_id,
        priority=priority,
        weight=weight,
        enabled=True,
        extra={},
        protocol=protocol,
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


def test_active_session_lease_overrides_new_session_order() -> None:
    engine = RoutingEngine(strategy="fallback_order")
    channels = [_channel(1, priority=10), _channel(2, priority=1)]

    decision = engine.route(
        channels,
        "model",
        preferred_channel_id=2,
    )

    assert [channel.id for channel in decision.candidates] == [2, 1]
    assert decision.lease_channel_id == 2
    assert decision.lease_used is True


def test_session_lease_never_overrides_cooldown() -> None:
    engine = RoutingEngine()
    channels = [_channel(1), _channel(2)]
    engine.mark_unavailable("model", channels[0], cooldown_seconds=60)

    decision = engine.route(
        channels,
        "model",
        preferred_channel_id=1,
    )

    assert [channel.id for channel in decision.candidates] == [2]
    assert decision.lease_channel_id == 1
    assert decision.lease_used is False


def test_cooldown_does_not_make_only_compatible_channel_unroutable() -> None:
    engine = RoutingEngine()
    channel = _channel(1, protocol="openai_responses")

    engine.mark_unavailable("model", channel, cooldown_seconds=60)
    decision = engine.route(
        [channel],
        "model",
        required_capabilities={"stream", "function_call"},
    )

    assert [candidate.id for candidate in decision.candidates] == [1]


def test_zero_cooldown_immediately_restores_channel() -> None:
    engine = RoutingEngine()
    channels = [_channel(1, priority=10), _channel(2, priority=1)]

    engine.mark_unavailable("model", channels[0], cooldown_seconds=0)
    decision = engine.route(channels, "model", affinity_key="session-a")

    assert decision.candidates[0].id == 1


def test_routing_diagnostics_report_capability_and_cooldown_reasons() -> None:
    engine = RoutingEngine()
    channel = _channel(1, protocol="openai")
    channel.extra = {"capabilities": ["stream"]}
    engine.mark_unavailable("model", channel, cooldown_seconds=60)

    diagnostic = engine.diagnose(
        [channel],
        "model",
        {"stream", "responses_native"},
    )[0]

    assert diagnostic["capabilities_match"] is False
    assert diagnostic["cooldown_seconds"] > 0
    assert diagnostic["reasons"] == ["capability_mismatch", "cooldown"]


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


def test_responses_native_capability_only_routes_native_protocol() -> None:
    engine = RoutingEngine()
    channels = [
        _channel(1, protocol="openai"),
        _channel(2, protocol="anthropic"),
        _channel(3, protocol="openai_responses"),
    ]

    decision = engine.route(
        channels,
        "model",
        required_capabilities={"responses_native"},
    )

    assert [channel.id for channel in decision.candidates] == [3]


def test_native_responses_protocol_inherently_supports_stream_and_tools() -> None:
    engine = RoutingEngine()
    native = _channel(1, protocol="openai_responses")
    native.extra = {"capabilities": []}
    chat = _channel(2, protocol="openai")
    chat.extra = {"capabilities": []}

    decision = engine.route(
        [chat, native],
        "model",
        required_capabilities={"stream", "function_call"},
    )

    assert [channel.id for channel in decision.candidates] == [1]


def test_adaptive_strategy_prefers_reliable_low_latency_channel() -> None:
    engine = RoutingEngine(strategy="adaptive")
    slow_unreliable = _channel(1)
    fast_reliable = _channel(2)

    for _ in range(8):
        engine.observe_result(
            "model", slow_unreliable, success=False, latency_ms=4_000
        )
        engine.observe_result(
            "model", fast_reliable, success=True, latency_ms=300
        )

    decision = engine.route([slow_unreliable, fast_reliable], "model")

    assert [channel.id for channel in decision.candidates] == [2, 1]
    assert decision.scores is not None
    assert decision.scores[2]["score"] > decision.scores[1]["score"]


def test_adaptive_strategy_never_crosses_priority_tiers() -> None:
    engine = RoutingEngine(strategy="adaptive")
    unhealthy_high_priority = _channel(1, priority=10)
    healthy_low_priority = _channel(2, priority=1)

    for _ in range(20):
        engine.observe_result(
            "model", unhealthy_high_priority, success=False, latency_ms=10_000
        )
        engine.observe_result(
            "model", healthy_low_priority, success=True, latency_ms=100
        )

    decision = engine.route(
        [healthy_low_priority, unhealthy_high_priority], "model"
    )

    assert decision.candidates[0].id == 1


def test_adaptive_load_penalizes_inflight_channel() -> None:
    engine = RoutingEngine(strategy="adaptive")
    busy = _channel(1)
    idle = _channel(2)

    engine.begin_attempt("model", busy)
    decision = engine.route([busy, idle], "model")

    assert decision.candidates[0].id == 2
    assert decision.scores[2]["load_utility"] == 1.0
    assert decision.scores[1]["load_utility"] == 0.5

    engine.observe_result("model", busy, success=True, latency_ms=200)
    assert engine.adaptive.score("model", busy).load_utility == 1.0


def test_adaptive_strategy_uses_affinity_as_score_tiebreaker() -> None:
    engine = RoutingEngine(strategy="adaptive")
    channels = [_channel(1), _channel(2), _channel(3)]

    first = engine.route(channels, "model", affinity_key="session-a")
    second = engine.route(
        list(reversed(channels)), "model", affinity_key="session-a"
    )

    assert [channel.id for channel in first.candidates] == [
        channel.id for channel in second.candidates
    ]
