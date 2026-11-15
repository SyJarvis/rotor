"""OpenCode partial subscription snapshots and their routing/API boundaries."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from itertools import product
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.openai import OpenAIAdapter
from rotor.adapters.protocol.responses import OpenAIResponsesAdapter
from rotor.api.admin import channels as channels_api
from rotor.api.v1.usage import router as usage_router
from rotor.channels.presets import list_provider_presets, provider_defaults
from rotor.config import settings
from rotor.database import Base, get_db
from rotor.gateway.routing import routing_engine
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.services import opencode_quota, quota_refresher, usage_cache
from rotor.services.opencode_quota import OpenCodeQuotaError, fetch_opencode_usage, is_opencode_quota_channel

USAGE = "/zen/go/v1/usage"
SUB = "/console/api/go/status"
BILLING = "/console/api/billing/status"
NOW = datetime.fromisoformat("2026-10-30T06:03:18+00:00").timestamp()
TOKEN = settings.API_KEY_PREFIX + "opencode-test"
SECRET = "private-provider-key"
MISSING = object()


def _replies():
    # Usage is the full 2026-10-30 response; subscription is its sanitized field selection.
    return {
        USAGE: {"usage": {
            "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-10-30T11:03:18.213Z"},
            "weekly": {"status": "ok", "percent": 0, "resetsAt": "2026-11-02T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 0, "resetsAt": "2026-11-30T03:01:14.000Z"},
        }},
        SUB: {
            "product": "go", "renewalProduct": "go", "useBalance": False,
            "cancelAtPeriodEnd": False, "renewalPending": False, "resumability": "renewing",
            "access": {
                "startsAt": "2026-10-30T03:01:14.000Z", "endsAt": "2026-11-30T03:01:14.000Z",
                "cancelAtPeriodEnd": False,
                "meters": {
                    "fiveHour": {"startsAt": None, "resetsAt": None,
                                 "limitMicroCents": "1200000000", "usedMicroCents": "0"},
                    "week": {"startsAt": "2026-10-26T00:00:00.000Z", "resetsAt": "2026-11-02T00:00:00.000Z",
                             "limitMicroCents": "3000000000", "usedMicroCents": "0"},
                    "month": {"resetsAt": "2026-11-30T03:01:14.000Z",
                              "limitMicroCents": "6000000000", "usedMicroCents": "0"},
                },
            },
        },
        # Billing success is a synthetic fixture from the official schema, not a live response.
        BILLING: {"billingMode": "prepaid", "mode": "pay-as-you-go", "balanceMicroCents": "9007199254740993",
                  "availableMicroCents": "9007199254740993", "creditLimitMicroCents": None,
                  "canPurchaseCredits": True, "canEnableAutoRecharge": True},
    }


def _channel(**changes):
    return SimpleNamespace(**{
        "id": 601, "name": "arbitrary-name", "type": "opencode", "auth_type": "api_key",
        "base_url": "https://opencode.ai/zen/go/v1", "key": SECRET,
        "extra": {"headers": {"Authorization": "override-secret", "x-org-id": "private-org",
                              "x-opencode-org-id": "private-org", "X-Private": "extra-secret"}},
        "enabled": True, "models": ["paid", "free", "other"], "priority": 1,
        "weight": 1, "protocol": "openai", **changes,
    })


def _handler(replies, requests):
    def handle(request):
        requests.append(request)
        reply = replies[request.url.path]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, content="null") if reply is None else httpx.Response(200, json=reply)
    return handle


def _fetch(monkeypatch, replies=None, *, now=NOW, channel=None, requests=None):
    monkeypatch.setattr(opencode_quota, "time", SimpleNamespace(time=lambda: now))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_handler(_replies() if replies is None else replies,
                                                  [] if requests is None else requests)),
            follow_redirects=True,
        ) as client:
            return await fetch_opencode_usage(client, channel or _channel())
    return asyncio.run(exercise())


def _set(replies, path, value):
    current = replies
    for part in path[:-1]:
        current = current[part]
    if value is MISSING:
        current.pop(path[-1])
    else:
        current[path[-1]] = value


def test_recorded_partial_snapshot_and_fixed_origin_headers(monkeypatch):
    replies, requests = _replies(), []
    replies[BILLING] = httpx.Response(403, content=SECRET)
    replies[SUB].update(subscriberUserId="private-person", paymentMethodId="private-payment")
    snapshot = _fetch(monkeypatch, replies, requests=requests)
    assert snapshot.plan == "go" and snapshot.subscription_status == "active"
    assert snapshot.allowed is None and snapshot.limit_reached is False
    assert snapshot.channel_enabled is None and snapshot.fetched_at == NOW
    assert snapshot.primary.window_seconds == 18000 and snapshot.secondary.window_seconds == 604800
    assert snapshot.primary.reset_at == pytest.approx(NOW + 18000.213)
    assert snapshot.primary.reset_after_seconds == 18000
    details = snapshot.opencode
    assert details.sources.model_dump() == {"usage": "ok", "subscription": "ok", "billing": "forbidden"}
    assert details.billing is None and details.subscription.use_balance is False
    assert details.subscription.access.five_hour.starts_at is None
    assert details.subscription.access.five_hour.reset_at is None
    assert details.subscription.access.monthly.limit_microcents == "6000000000"
    assert details.subscription.access.monthly.starts_at is None
    assert details.usage.monthly.status == "ok" and details.usage.monthly.used_percent == 0
    assert {r.url.path for r in requests} == {USAGE, SUB, BILLING}
    for request in requests:
        assert request.method == "GET" and request.url.scheme == "https" and request.url.host == "opencode.ai"
        assert not request.url.query and not request.content
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        assert request.headers["accept"] == "application/json"
        assert not {"x-org-id", "x-opencode-org-id", "x-private"} & set(request.headers)
        assert set(request.extensions["timeout"].values()) == {15.0}
    serialized = snapshot.model_dump_json()
    assert all(value not in serialized for value in (SECRET, "private-person", "private-payment", "private-org"))


@pytest.mark.parametrize("url", [
    "https://opencode.ai/zen/go/v1", "https://opencode.ai:443/zen/go/v1/", "https://OPENCODE.AI/zen/go/v1",
])
def test_accepted_channel_urls(url):
    assert is_opencode_quota_channel(_channel(base_url=url))


@pytest.mark.parametrize("changes", [
    {"type": kind} for kind in ("openai", "deepseek", "anthropic", "command", "zhipu", "OpenCode", None)
] + [
    {"auth_type": kind} for kind in ("oauth", "bearer", None)
] + [
    {"base_url": url} for url in (
        "http://opencode.ai/zen/go/v1", "https://opencode.ai:8443/zen/go/v1", "https://opencode.ai:bad/zen/go/v1",
        "https://opencode.ai.evil.test/zen/go/v1", "https://evil.test/zen/go/v1", "https://opencode.ai./zen/go/v1",
        "https://user@opencode.ai/zen/go/v1", "https://opencode.ai@evil.test/zen/go/v1",
        "https://opencode.ai/zen/v1", "https://opencode.ai/inference/v1", "https://opencode.ai/zen/go/v1//",
        "https://opencode.ai/zen/go/v1/models", "https://opencode.ai/zen/go/v1?x=1",
        "https://opencode.ai/zen/go/v1#x", "https://opencode.ai/zen/go/v1?", "https://opencode.ai/zen/go/v1#",
        "https://open\ncode.ai/zen/go/v1", " https://opencode.ai/zen/go/v1", "https://[invalid", None,
    )
])
def test_wrong_provider_origin_path_or_auth_never_queries(changes):
    channel = _channel(**changes)
    assert not is_opencode_quota_channel(channel)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("unsupported channel must not send a request"),
        )) as client:
            with pytest.raises(OpenCodeQuotaError):
                await fetch_opencode_usage(client, channel)
            if channel.auth_type != "oauth":
                assert await quota_refresher.fetch_usage(channel, client) is None
    asyncio.run(exercise())


@pytest.mark.parametrize("key", [None, "", "  ", 123])
def test_missing_key_is_rejected_before_requests(monkeypatch, key):
    requests = []
    with pytest.raises(OpenCodeQuotaError, match="API key is missing"):
        _fetch(monkeypatch, channel=_channel(key=key), requests=requests)
    assert requests == []


@pytest.mark.parametrize("key", ["private\nkey", "private\tkey", "private key", "私密key"])
def test_invalid_header_key_has_a_safe_error_without_requests(monkeypatch, key):
    requests = []
    with pytest.raises(OpenCodeQuotaError) as error:
        _fetch(monkeypatch, channel=_channel(key=key), requests=requests)
    assert str(error.value) == "OpenCode API key has an invalid format"
    assert key not in str(error.value) and requests == []


@pytest.mark.parametrize(("usage_ok", "sub_ok", "billing_ok"), list(product((False, True), repeat=3)))
def test_sources_succeed_independently_and_balance_alone_is_not_a_go_snapshot(monkeypatch, usage_ok, sub_ok, billing_ok):
    replies = _replies()
    for path, ok in [(USAGE, usage_ok), (SUB, sub_ok), (BILLING, billing_ok)]:
        if not ok:
            replies[path] = httpx.Response(403, content=SECRET)
    if not usage_ok and not sub_ok:
        with pytest.raises(OpenCodeQuotaError) as error:
            _fetch(monkeypatch, replies)
        assert error.value.status_code == 502 and SECRET not in str(error.value)
        return
    result = _fetch(monkeypatch, replies)
    assert result.opencode.sources.model_dump() == {
        "usage": "ok" if usage_ok else "forbidden",
        "subscription": "ok" if sub_ok else "forbidden",
        "billing": "ok" if billing_ok else "forbidden",
    }
    assert (result.opencode.usage is not None) is usage_ok
    assert (result.opencode.subscription is not None) is sub_ok
    assert (result.opencode.billing is not None) is billing_ok
    assert (result.primary is not None) is usage_ok
    assert result.limit_reached is (False if usage_ok else None)
    assert result.subscription_status == ("active" if sub_ok else None)
    assert result.plan == ("go" if sub_ok else None)


@pytest.mark.parametrize(("reply", "state"), [
    (httpx.Response(401, content=SECRET), "unauthorized"),
    (httpx.Response(403, content=SECRET), "forbidden"),
    (httpx.Response(429, content=SECRET), "unavailable"),
    (httpx.Response(503, content=SECRET), "unavailable"),
    (httpx.ConnectError(SECRET), "unavailable"),
    (httpx.ReadTimeout(SECRET), "unavailable"),
    (TimeoutError(SECRET), "unavailable"),
    (httpx.Response(302, headers={"Location": "https://evil.test/"}), "error"),
    (httpx.Response(404, content=SECRET), "error"),
    (httpx.Response(200, content=SECRET), "invalid_response"),
    ({"success": False, "error": SECRET}, "invalid_response"),
    ({"error": SECRET}, "invalid_response"),
    ([], "invalid_response"),
])
@pytest.mark.parametrize(("path", "name"), [(USAGE, "usage"), (SUB, "subscription"), (BILLING, "billing")])
def test_source_failures_are_classified_and_never_leak_or_follow_redirects(monkeypatch, reply, state, path, name):
    replies, requests = _replies(), []
    replies[path] = reply
    result = _fetch(monkeypatch, replies, requests=requests)
    assert getattr(result.opencode.sources, name) == state
    assert getattr(result.opencode, name) is None
    assert len(requests) == 3
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize(("usage_reply", "sub_reply", "status"), [
    (httpx.ReadTimeout(SECRET), httpx.Response(503), 503),
    (httpx.Response(429), httpx.ConnectError(SECRET), 503),
    (httpx.Response(401), httpx.Response(503), 502),
    ({}, httpx.ReadTimeout(SECRET), 502),
])
def test_all_go_sources_fail_with_safe_aggregate_error(monkeypatch, usage_reply, sub_reply, status):
    replies = _replies()
    replies[USAGE], replies[SUB] = usage_reply, sub_reply
    with pytest.raises(OpenCodeQuotaError) as error:
        _fetch(monkeypatch, replies)
    assert error.value.status_code == status
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("null_access", [False, True])
def test_explicit_null_subscription_or_access_is_inactive_not_free(monkeypatch, null_access):
    replies = _replies()
    if null_access:
        replies[SUB]["access"] = None
    else:
        replies[SUB] = None
    replies[USAGE] = httpx.Response(403)
    result = _fetch(monkeypatch, replies)
    assert result.opencode.sources.subscription == "ok"
    assert result.subscription_status == "inactive" and result.allowed is None
    assert result.limit_reached is None and result.primary is None
    assert result.plan == ("go" if null_access else None)


@pytest.mark.parametrize(("moment", "status"), [
    ("2026-10-30T03:01:13.999+00:00", "pending"),
    ("2026-10-30T03:01:14+00:00", "active"),
    ("2026-11-30T03:01:13.999+00:00", "active"),
    ("2026-11-30T03:01:14+00:00", "expired"),
])
def test_access_state_uses_period_boundaries_not_renewal_flags(monkeypatch, moment, status):
    replies = _replies()
    replies[SUB].update(cancelAtPeriodEnd=True, renewalPending=True, resumability="unknown-future-status")
    result = _fetch(monkeypatch, replies, now=datetime.fromisoformat(moment).timestamp())
    assert result.subscription_status == status
    assert result.opencode.subscription.resumability == "unknown-future-status"


def test_unknown_products_and_billing_modes_preserve_success(monkeypatch):
    replies = _replies()
    replies[SUB].update(product="future-go", renewalProduct="future-team", resumability="future-resume")
    replies[BILLING].update(billingMode="future-billing", mode="future-mode")
    result = _fetch(monkeypatch, replies)
    assert result.plan == "future-go" and result.subscription_status == "active"
    assert result.opencode.subscription.renewal_product == "future-team"
    assert result.opencode.billing.billing_mode == "future-billing"
    assert result.opencode.billing.mode == "future-mode"
    replies[SUB].pop("resumability")
    assert _fetch(monkeypatch, replies).opencode.subscription.resumability is None


@pytest.mark.parametrize("value", ["0", "00012", "900719925474099312345678901234567890"])
def test_microcents_remain_exact_strings_without_percentage_fabrication(monkeypatch, value):
    replies = _replies()
    replies[SUB]["access"]["meters"]["fiveHour"].update(limitMicroCents=value, usedMicroCents=value)
    replies[BILLING]["balanceMicroCents"] = value
    result = _fetch(monkeypatch, replies)
    assert result.opencode.subscription.access.five_hour.limit_microcents == value
    assert result.opencode.subscription.access.five_hour.used_microcents == value
    assert result.opencode.billing.balance_microcents == value
    assert result.primary.used_percent == 0 and result.limit_reached is False


def test_negative_ledger_balances_and_nullable_credit_limit(monkeypatch):
    replies = _replies()
    replies[BILLING].update(balanceMicroCents="-9007199254740993", availableMicroCents="-1", creditLimitMicroCents="0")
    result = _fetch(monkeypatch, replies)
    assert result.opencode.billing.balance_microcents == "-9007199254740993"
    assert result.opencode.billing.available_microcents == "-1"
    assert result.opencode.billing.credit_limit_microcents == "0"


@pytest.mark.parametrize("value", [True, 1, 1.2, None, "", "+1", "1.2", "1e8", " 1", "1\n", "１２", "NaN", "Inf"])
@pytest.mark.parametrize("path", [(SUB, "access", "meters", "month", "usedMicroCents"), (BILLING, "balanceMicroCents")])
def test_illegal_money_never_becomes_zero(monkeypatch, value, path):
    replies = _replies()
    _set(replies, path, value)
    result = _fetch(monkeypatch, replies)
    name = "subscription" if path[0] == SUB else "billing"
    assert getattr(result.opencode.sources, name) == "invalid_response"
    assert getattr(result.opencode, name) is None


@pytest.mark.parametrize("path", [
    (SUB, "access"), (SUB, "access", "meters", "fiveHour", "startsAt"),
    (SUB, "access", "meters", "fiveHour", "resetsAt"), (BILLING, "creditLimitMicroCents"),
])
def test_required_nullable_fields_cannot_be_missing(monkeypatch, path):
    replies = _replies()
    _set(replies, path, MISSING)
    result = _fetch(monkeypatch, replies)
    name = "subscription" if path[0] == SUB else "billing"
    assert getattr(result.opencode.sources, name) == "invalid_response"


@pytest.mark.parametrize("value", [None, True, "1", -1, 101, float("nan"), float("inf"), float("-inf")])
def test_usage_percent_is_finite_numeric_and_in_range(monkeypatch, value):
    import json

    replies = _replies()
    replies[USAGE]["usage"]["monthly"]["percent"] = value
    # Preserve all three windows while allowing simulated nonstandard NaN/Infinity JSON.
    replies[USAGE] = httpx.Response(200, content=json.dumps(replies[USAGE]))
    result = _fetch(monkeypatch, replies)
    assert result.opencode.sources.usage == "invalid_response" and result.limit_reached is None


@pytest.mark.parametrize("path", [
    (USAGE, "usage", "monthly", "resetsAt"), (SUB, "access", "startsAt"),
    (SUB, "access", "meters", "fiveHour", "resetsAt"),
])
@pytest.mark.parametrize("value", ["2026-10-30T03:01:14", "invalid-date", 123, True, "2026-13-01T00:00:00Z"])
def test_invalid_or_timezone_less_dates_are_source_errors(monkeypatch, path, value):
    replies = _replies()
    _set(replies, path, value)
    result = _fetch(monkeypatch, replies)
    assert getattr(result.opencode.sources, "usage" if path[0] == USAGE else "subscription") == "invalid_response"


def test_offsets_fractional_times_and_past_resets(monkeypatch):
    replies = _replies()
    replies[USAGE]["usage"]["rolling"]["resetsAt"] = "2026-10-30T19:03:18.213+08:00"
    replies[USAGE]["usage"]["weekly"]["resetsAt"] = "2026-10-29T00:00:00Z"
    replies[SUB]["access"]["meters"]["fiveHour"].update(
        startsAt="2026-10-30T06:00:00.500Z", resetsAt="2026-10-30T11:00:00.500Z",
    )
    result = _fetch(monkeypatch, replies)
    assert result.primary.reset_at == pytest.approx(NOW + 18000.213)
    assert result.secondary.reset_after_seconds == 0
    assert result.opencode.subscription.access.five_hour.reset_at % 1 == 0.5


@pytest.mark.parametrize(("path", "value"), [
    ((SUB, "access", "endsAt"), "2026-10-30T03:01:14.000Z"),
    ((SUB, "access", "endsAt"), "2026-10-29T03:01:14.000Z"),
    ((SUB, "useBalance"), 1), ((SUB, "renewalPending"), "false"), ((SUB, "cancelAtPeriodEnd"), None),
    ((SUB, "product"), ""), ((SUB, "resumability"), None),
    ((SUB, "access", "meters", "month", "limitMicroCents"), "-1"),
    ((BILLING, "creditLimitMicroCents"), "-1"), ((BILLING, "canPurchaseCredits"), 0),
    ((USAGE, "usage", "monthly"), MISSING), ((USAGE, "usage", "weekly", "status"), "new-state"),
])
def test_malformed_source_fields_are_not_treated_as_missing_subscription(monkeypatch, path, value):
    replies = _replies()
    _set(replies, path, value)
    result = _fetch(monkeypatch, replies)
    name = {SUB: "subscription", BILLING: "billing", USAGE: "usage"}[path[0]]
    assert getattr(result.opencode.sources, name) == "invalid_response"
    if path[0] == SUB:
        assert result.subscription_status is None


@pytest.mark.parametrize("window", ["rolling", "weekly", "monthly"])
def test_admin_limit_reached_uses_each_window_status_not_amounts(monkeypatch, window):
    replies = _replies()
    replies[USAGE]["usage"][window].update(status="rate-limited", percent=12)
    replies[SUB]["useBalance"] = True
    result = _fetch(monkeypatch, replies)
    assert result.limit_reached is True and result.allowed is None
    field = {"rolling": "five_hour", "weekly": "weekly", "monthly": "monthly"}[window]
    assert getattr(result.opencode.usage, field).status == "rate-limited"
    replies[USAGE]["usage"][window].update(status="ok", percent=100)
    result = _fetch(monkeypatch, replies)
    assert result.limit_reached is False and getattr(result.opencode.usage, field).status == "ok"


@pytest.mark.parametrize("limited", [False, True])
def test_snapshot_never_creates_or_clears_cooldowns(monkeypatch, limited):
    replies = _replies()
    replies[USAGE]["usage"]["monthly"]["status"] = "rate-limited" if limited else "ok"
    replies[SUB]["useBalance"] = True
    channel, snapshot = _channel(), _fetch(monkeypatch, replies)
    try:
        routing_engine.mark_unavailable("paid", channel, 300, reason="plan_limit")
        routing_engine.mark_unavailable("other", channel, 300)
        quota_refresher.apply_usage(channel, snapshot)
        assert usage_cache.fresh(channel.id) is snapshot and channel.enabled is True
        for model, expected in [("paid", True), ("other", True), ("free", False)]:
            assert ("cooldown" in routing_engine.diagnose([channel], model, required_capabilities=set())[0]["reasons"]) is expected
    finally:
        routing_engine.clear_cooldown(None, channel.id)
        usage_cache._entries.clear()


@asynccontextmanager
async def _api_client(monkeypatch, replies, requests, *, provider_type="opencode", enabled=True, allowed_channels=None):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        for cid in (601, 602):
            db.add(Channel(id=cid, name=f"go-{cid}", type=provider_type, auth_type="api_key", key=SECRET,
                           base_url="https://opencode.ai/zen/go/v1", enabled=enabled,
                           models=["paid", "free", "other"], model_mapping={}, extra={}))
        db.add(Token(key=TOKEN, name="opencode-query", enabled=True, allowed_channels=allowed_channels))
        await db.commit()
    app = FastAPI()
    app.include_router(channels_api.router, prefix="/api/admin")
    app.include_router(usage_router, prefix="/v1")

    async def get_session():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = get_session
    client_class = httpx.AsyncClient
    monkeypatch.setattr(channels_api.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(_handler(replies, requests)), **kw,
    ))
    usage_cache._entries.clear()
    try:
        async with client_class(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions
    finally:
        usage_cache._entries.clear()
        for cid in (601, 602):
            routing_engine.clear_cooldown(None, cid)
        await engine.dispose()


def test_admin_partial_cache_ttl_refresh_and_gateway_permissions(monkeypatch):
    replies, requests, clock = _replies(), [], [NOW]
    for module in (opencode_quota, channels_api, usage_cache):
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0]))

    async def exercise():
        async with _api_client(monkeypatch, replies, requests, allowed_channels=[601]) as (client, sessions):
            path = "/api/admin/channels/601/oauth/usage"
            first = await client.get(path)
            assert first.status_code == 200 and first.json()["channel_enabled"] is True
            clock[0] += 89
            assert (await client.get(path)).json() == first.json() and len(requests) == 3
            clock[0] += 1
            assert (await client.get(path)).json()["fetched_at"] == clock[0] and len(requests) == 6
            replies[BILLING] = httpx.Response(403, content=SECRET)
            partial = await client.get(path + "?refresh=true")
            assert partial.status_code == 200 and len(requests) == 9
            assert partial.json()["opencode"]["billing"] is None
            assert partial.json()["opencode"]["sources"]["billing"] == "forbidden"
            usage_cache.store(602, usage_cache.fresh(601))
            gateway = await client.get("/v1/usage", headers={"Authorization": f"Bearer {TOKEN}"})
            assert gateway.status_code == 200 and len(requests) == 9
            entries = gateway.json()["channels"]
            assert [e["channel_id"] for e in entries] == [601]
            assert entries[0]["provider"] == "opencode"
            assert entries[0]["opencode"] == {"usage": partial.json()["opencode"]["usage"]}
            assert entries[0]["limit_reached"] is None
            assert all(field not in gateway.text for field in ("billing", "balance_microcents"))
            assert entries[0]["rate_limit"]["primary_window"] == partial.json()["primary"]
            assert (await client.get("/v1/usage")).status_code == 401
            old = usage_cache._entries[601]
            replies[USAGE] = httpx.Response(401, content=SECRET)
            replies[SUB] = httpx.Response(401, content=SECRET)
            clock[0] += 10
            failed = await client.get(path + "?refresh=true")
            assert failed.status_code == 502 and SECRET not in failed.text
            assert usage_cache._entries[601] == old
            assert (await client.get(path)).json() == partial.json()
            clock[0] += 80
            assert (await client.get(path)).status_code == 502
            assert usage_cache.fresh(601) is None
            async with sessions() as db:
                assert (await db.get(Channel, 601)).enabled is True
    asyncio.run(exercise())


@pytest.mark.parametrize("provider_type", ["openai", "deepseek", "anthropic", "zhipu", "command"])
def test_wrong_provider_cannot_report_old_cache_or_query_upstream(monkeypatch, provider_type):
    snapshot = _fetch(monkeypatch)
    replies, requests = _replies(), []

    async def exercise():
        async with _api_client(monkeypatch, replies, requests, provider_type=provider_type) as (client, sessions):
            usage_cache.store(601, snapshot)
            result = await client.get("/v1/usage", headers={"Authorization": f"Bearer {TOKEN}"})
            assert result.status_code == 200 and result.json()["channels"] == []
            for query in ("", "?refresh=true"):
                assert (await client.get("/api/admin/channels/601/oauth/usage" + query)).status_code == 400
            assert not requests and usage_cache.fresh(601) is None
    asyncio.run(exercise())


@pytest.mark.parametrize(("reply", "expected"), [(httpx.ReadTimeout(SECRET), 503), (httpx.Response(403), 502)])
def test_admin_all_fail_does_not_cache_or_modify_disabled_channel(monkeypatch, reply, expected):
    replies, requests = _replies(), []
    replies[USAGE] = replies[SUB] = reply

    async def exercise():
        async with _api_client(monkeypatch, replies, requests, enabled=False) as (client, sessions):
            response = await client.get("/api/admin/channels/601/oauth/usage")
            assert response.status_code == expected and SECRET not in response.text
            assert usage_cache.fresh(601) is None
            replies[SUB] = None
            response = await client.get("/api/admin/channels/601/oauth/usage?refresh=true")
            assert response.status_code == 200 and response.json()["subscription_status"] == "inactive"
            assert response.json()["channel_enabled"] is False
            async with sessions() as db:
                assert (await db.get(Channel, 601)).enabled is False
    asyncio.run(exercise())


def test_background_dispatch_keeps_enabled_and_cooldowns(monkeypatch):
    replies, requests = _replies(), []
    replies[USAGE]["usage"]["monthly"]["status"] = "rate-limited"
    replies[SUB]["useBalance"] = True

    async def exercise():
        async with _api_client(monkeypatch, replies, requests) as (client, sessions):
            monkeypatch.setattr(quota_refresher, "async_session_maker", sessions)
            async with sessions() as db:
                channel = await db.get(Channel, 601)
                routing_engine.mark_unavailable("paid", channel, 300, reason="plan_limit")
                routing_engine.mark_unavailable("other", channel, 300)
            await quota_refresher.QuotaRefresher()._refresh(601)
            assert len(requests) == 3 and usage_cache.fresh(601).limit_reached is True
            replies[SUB] = None
            assert (await client.get("/api/admin/channels/601/oauth/usage?refresh=true")).json()["subscription_status"] == "inactive"
            async with sessions() as db:
                channel = await db.get(Channel, 601)
                assert channel.enabled is True
                for model, expected in [("paid", True), ("other", True), ("free", False)]:
                    assert ("cooldown" in routing_engine.diagnose([channel], model, required_capabilities=set())[0]["reasons"]) is expected
    asyncio.run(exercise())


def test_opencode_preset_and_adapter_keep_responses_protocol():
    preset = next(p for p in list_provider_presets() if p["id"] == "opencode")
    assert preset["label"] == "OpenCode" and preset["base_url"] == "https://opencode.ai/zen/go/v1"
    assert preset["auth_type"] == "bearer" and preset["request_path"] == "/chat/completions"
    assert preset["models_path"] == "/models" and preset["protocol"] == "openai"
    channel = _channel(extra={})
    assert type(AdapterFactory.create_adapter(channel, None)) is OpenAIAdapter
    channel.protocol = "openai_responses"
    assert type(AdapterFactory.create_adapter(channel, None)) is OpenAIResponsesAdapter
    assert provider_defaults("opencode", "openai_responses")["request_path"] == "/responses"
    assert "opencode" in AdapterFactory.get_supported_providers()


@pytest.mark.parametrize("window", ["rolling", "weekly", "monthly"])
def test_gateway_keeps_limited_window_but_redacts_billing(monkeypatch, window):
    replies, requests = _replies(), []
    replies[USAGE]["usage"][window].update(status="rate-limited", percent=100)
    replies[SUB]["useBalance"] = True

    async def exercise():
        async with _api_client(monkeypatch, replies, requests, allowed_channels=[601]) as (client, sessions):
            admin = await client.get("/api/admin/channels/601/oauth/usage")
            assert admin.status_code == 200 and admin.json()["limit_reached"] is True
            assert admin.json()["opencode"]["billing"]["balance_microcents"] == "9007199254740993"
            gateway = await client.get("/v1/usage", headers={"Authorization": f"Bearer {TOKEN}"})
            assert gateway.status_code == 200 and len(requests) == 3
            entry = gateway.json()["channels"][0]
            field = {"rolling": "five_hour", "weekly": "weekly", "monthly": "monthly"}[window]
            assert entry["limit_reached"] is None and entry["allowed"] is None
            assert entry["opencode"]["usage"][field]["status"] == "rate-limited"
            assert set(entry["opencode"]) == {"usage"}
            assert all(secret not in gateway.text for secret in (
                SECRET, "9007199254740993", "balance_microcents", "used_microcents", "use_balance", "billing",
            ))
    asyncio.run(exercise())
