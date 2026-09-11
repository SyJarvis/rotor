import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from rotor.core.exceptions import ChannelsTemporarilyUnavailable, api_router_exception_handler
from rotor.gateway.routing import RoutingEngine


def channel(channel_id=1):
    return SimpleNamespace(id=channel_id, name=f"channel-{channel_id}", type="openai", protocol="openai", enabled=True, priority=1, weight=1, extra={})


@pytest.fixture
def state():
    now = [100.0]
    return RoutingEngine("fallback_order", clock=lambda: now[0]), now, channel()


def test_all_cooling_channels_have_no_candidates_and_retry_after_rounds_up(state):
    engine, now, first = state
    second = channel(2)
    engine.mark_unavailable("model", first, 2.1)
    engine.mark_unavailable("model", second, 10)
    decision = engine.route([first, second], "model", preferred_channel_id=first.id)
    assert decision.candidates == [] and decision.temporarily_unavailable
    assert decision.retry_after_seconds == 3
    assert not decision.lease_used
    assert not engine.diagnose([first], "model")[0]["routable"]
    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
    response = asyncio.run(api_router_exception_handler(request, ChannelsTemporarilyUnavailable("model", decision.retry_after_seconds)))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    assert b"channels_temporarily_unavailable" in response.body


def test_capability_mismatch_is_distinct_from_temporary_unavailability(state):
    engine, _, first = state
    decision = engine.route([first], "model", required_capabilities={"responses_native"})
    assert decision.candidates == []
    assert not decision.temporarily_unavailable
    assert decision.retry_after_seconds is None


def test_route_and_diagnose_do_not_claim_or_clear_expired_cooldown(state):
    engine, now, first = state
    engine.mark_unavailable("model", first, 5)
    now[0] += 5
    for _ in range(3):
        assert engine.route([first], "model").candidates == [first]
        assert engine.diagnose([first], "model")[0]["recovery_probe_eligible"]
        assert engine.available_native_channel_ids([first], "model", "openai_chat", set()) == {first.id}
    permit = engine.admit_attempt("model", first)
    assert permit.is_probe
    assert engine.admit_attempt("model", first) is None


def test_stale_candidate_lists_admit_one_concurrent_probe(state):
    engine, now, first = state
    engine.mark_unavailable("model", first, 0)
    stale = engine.route([first], "model").candidates
    with ThreadPoolExecutor(max_workers=16) as pool:
        permits = list(pool.map(lambda _: engine.admit_attempt("model", stale[0]), range(64)))
    admitted = [permit for permit in permits if permit is not None]
    assert len(admitted) == 1
    now[0] += 100_000
    assert engine.admit_attempt("model", first) is None
    assert engine.route([first], "model").retry_after_seconds == 1
    assert "recovery_probe_inflight" in engine.diagnose([first], "model")[0]["reasons"]
    admitted[0].provider_succeeded = True
    engine.release_attempt(admitted[0])
    assert not engine.admit_attempt("model", first).is_probe


def test_healthy_requests_and_different_model_channel_probes_are_independent(state):
    engine, _, first = state
    second = channel(2)
    normal = [engine.admit_attempt("healthy", first) for _ in range(3)]
    assert all(permit is not None and not permit.is_probe for permit in normal)
    for model, candidate in [("a", first), ("b", first), ("a", second)]:
        engine.mark_unavailable(model, candidate, 0)
        assert engine.admit_attempt(model, candidate).is_probe


def test_success_restores_probe_even_if_accounting_never_observes(state):
    engine, _, first = state
    engine.mark_unavailable("model", first, 0)
    permit = engine.admit_attempt("model", first)
    permit.provider_succeeded = True
    engine.release_attempt(permit)
    engine.release_attempt(permit)
    assert engine.adaptive.score("model", first).load_utility == 1
    assert engine.adaptive.score("model", first).observations == 0
    assert not engine.admit_attempt("model", first).is_probe


@pytest.mark.parametrize("observed", [False, True])
def test_failed_or_cancelled_probe_releases_and_delays_retry(state, observed):
    engine, now, first = state
    engine.mark_unavailable("model", first, 0)
    permit = engine.admit_attempt("model", first)
    if observed:
        engine.observe_result("model", first, success=False, latency_ms=12, admission=permit)
    engine.release_attempt(permit)
    assert engine.admit_attempt("model", first) is None
    assert engine.route([first], "model").retry_after_seconds == 30
    now[0] += 30
    assert engine.admit_attempt("model", first).is_probe


def test_cancelled_healthy_attempt_does_not_start_cooldown(state):
    engine, _, first = state
    permit = engine.admit_attempt("model", first)
    engine.release_attempt(permit)
    assert not engine.admit_attempt("model", first).is_probe


@pytest.mark.parametrize("retry_after", [0, 17])
def test_new_failure_generation_survives_old_probe_success_or_release(state, retry_after):
    engine, now, first = state
    engine.mark_unavailable("model", first, 0)
    permit = engine.admit_attempt("model", first)
    engine.mark_unavailable("model", first, retry_after)
    permit.provider_succeeded = True
    engine.release_attempt(permit)
    if retry_after:
        assert engine.admit_attempt("model", first) is None
        now[0] += retry_after
    next_permit = engine.admit_attempt("model", first)
    assert next_permit.is_probe
    engine.release_attempt(permit)
    assert engine.admit_attempt("model", first) is None


def test_stale_healthy_success_cannot_clear_new_cooldown(state):
    engine, _, first = state
    healthy = engine.admit_attempt("model", first)
    engine.mark_unavailable("model", first, 60)
    healthy.provider_succeeded = True
    engine.release_attempt(healthy)
    assert engine.admit_attempt("model", first) is None


def test_later_failure_never_shortens_existing_retry_after(state):
    engine, now, first = state
    engine.mark_unavailable("model", first, 120)
    now[0] += 5
    engine.mark_unavailable("model", first)
    engine.mark_unavailable("model", first, 0)
    assert engine.route([first], "model").retry_after_seconds == 115


def test_busy_probe_preserves_new_known_retry_after(state):
    engine, _, first = state
    engine.mark_unavailable("model", first, 0)
    permit = engine.admit_attempt("model", first)
    engine.mark_unavailable("model", first, 120)
    assert engine.route([first], "model").retry_after_seconds == 120
    engine.release_attempt(permit)


def test_incompatible_or_disabled_channel_is_not_probe_eligible(state):
    engine, _, first = state
    engine.mark_unavailable("model", first, 0)
    assert not engine.diagnose([first], "model", {"responses_native"})[0]["recovery_probe_eligible"]
    first.enabled = False
    assert not engine.diagnose([first], "model")[0]["recovery_probe_eligible"]


def test_observation_and_release_each_finish_inflight_at_most_once(state):
    engine, _, first = state
    permit = engine.admit_attempt("model", first)
    other = engine.admit_attempt("model", first)
    engine.observe_result("model", first, success=True, latency_ms=20, admission=permit)
    engine.observe_result("model", first, success=False, latency_ms=1000, admission=permit)
    engine.release_attempt(permit)
    engine.release_attempt(permit)
    stats = engine.adaptive.score("model", first)
    assert stats.observations == 1 and stats.load_utility == 0.5
    engine.release_attempt(other)
    engine.observe_result("model", first, success=True, latency_ms=20, admission=other)
    assert engine.adaptive.score("model", first).observations == 1
    assert engine.adaptive.score("model", first).load_utility == 1


def test_reassessment_never_claims_probe_or_overrides_busy_native(state):
    engine, _, native = state
    fallback = channel(2)
    fallback.protocol = "anthropic"
    engine.mark_unavailable("model", native, 0)
    first = engine.route([native, fallback], "model", preferred_channel_id=2, lease_reassessment_due=True)
    assert first.lease_reassessment_attempted and first.candidates[0] is native
    permit = engine.admit_attempt("model", native)
    second = engine.route([native, fallback], "model", preferred_channel_id=2, lease_reassessment_due=True)
    assert not second.lease_reassessment_attempted and second.lease_used
    assert second.candidates == [fallback]
    engine.release_attempt(permit)
