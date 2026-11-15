"""Command Goat subscription queries, API integration, and routing isolation."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api.admin import channels as channels_api
from rotor.api.v1.usage import router as usage_router
from rotor.config import settings
from rotor.database import Base, get_db
from rotor.gateway.routing import routing_engine
from rotor.models.channel import Channel
from rotor.models.token import Token
from rotor.services import quota_refresher, usage_cache
from rotor.services.command_goat_quota import (
    CommandGoatQuotaError,
    fetch_command_goat_usage,
    is_command_goat_quota_channel,
    parse_command_goat_usage,
)

WHO = "/alpha/whoami"
SUB = "/alpha/billing/subscriptions"
CREDITS = "/alpha/billing/credits"
SUMMARY = "/alpha/usage/summary"
NOW = 1_800_000_000.0
KEY = settings.API_KEY_PREFIX + "command-goat-test"


def _payloads():
    # Sanitized fields recorded from the four successful queries on 2026-10-30.
    return {
        WHO: {"success": True, "org": None},
        SUB: {"success": True, "data": {
            "planId": "individual-goat", "status": "active",
            "currentPeriodStart": "2026-10-16T00:13:24.000Z",
            "currentPeriodEnd": "2026-11-16T00:13:24.000Z",
            "cancelAtPeriodEnd": False, "canceledAt": None, "endedAt": None,
        }},
        CREDITS: {
            "credits": {"belowThreshold": False, "creditThreshold": 0,
                        "monthlyCredits": 65.6947884845, "purchasedCredits": 0, "freeCredits": 0},
            "windowLimits": {
                "limited": True, "exceeded": None,
                "fiveHour": {"used": 0, "cap": 14, "exceeded": False, "resetAt": 0},
                "weekly": {"used": 0, "cap": 35, "exceeded": False, "resetAt": 0},
            },
            "sandboxAccess": True,
            "sandboxMinutes": {"usedMinutes": 0, "limitMinutes": 0, "overageUsd": 0,
                               "resetAt": 1793491200000},
        },
        SUMMARY: {"totalCount": 292, "totalCost": 4.3052115155,
                  "averageCost": 0.014743875053082191, "successRate": 100,
                  "completedCount": 292, "failedCount": 0, "totalCredits": 4.3052115155,
                  "totalFreeCredits": 0, "totalMonthlyCredits": 4.3052115155,
                  "totalPurchasedCredits": 0, "periodBasis": "billing-period",
                  "periodStart": "2026-10-16T00:13:24.000Z", "daily": []},
    }


def _channel(**overrides):
    return SimpleNamespace(**{
        "id": 501, "name": "arbitrary-name", "type": "command", "auth_type": "api_key",
        "base_url": "https://api.commandcode.ai/provider/v1", "key": "provider-private-key",
        "extra": {"headers": {"Authorization": "override-secret", "X-Private": "extra-secret"}},
        "enabled": True, "models": ["paid", "free", "other"], "priority": 1,
        "weight": 1, "protocol": "openai", **overrides,
    })


def _parse(payloads=None):
    payloads = payloads or _payloads()
    return parse_command_goat_usage(payloads[SUB], payloads[CREDITS], payloads[SUMMARY], fetched_at=NOW)


@pytest.mark.parametrize(("url", "expected"), [
    ("https://api.commandcode.ai/provider/v1", True),
    ("https://api.commandcode.ai:443/provider/v1/", True),
    ("https://API.COMMANDCODE.AI/provider/v1", True),
    ("http://api.commandcode.ai/provider/v1", False),
    ("https://api.commandcode.ai:8443/provider/v1", False),
    ("https://api.commandcode.ai.evil.test/provider/v1", False),
    ("https://evil.test/api.commandcode.ai", False),
    ("https://api.commandcode.ai@evil.test", False),
    ("https://secret@api.commandcode.ai", False),
    ("https://api.commandcode.ai:invalid", False),
    ("https://[invalid", False),
    (None, False),
])
def test_channel_matching_uses_exact_https_origin(url, expected):
    assert is_command_goat_quota_channel(_channel(base_url=url)) is expected
    assert not is_command_goat_quota_channel(_channel(base_url=url, auth_type="oauth"))


@pytest.mark.parametrize("provider_type", ["openai", "anthropic", "zhipu", "unknown", None])
def test_command_queries_require_explicit_provider_selection(provider_type):
    channel = _channel(type=provider_type)
    assert not is_command_goat_quota_channel(channel)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("wrong provider must not query upstream"),
        )) as client:
            assert await quota_refresher.fetch_usage(channel, client) is None
            with pytest.raises(CommandGoatQuotaError):
                await fetch_command_goat_usage(client, channel)
    asyncio.run(exercise())


def test_actual_response_preserves_subscription_credits_and_window_units():
    usage = _parse()
    assert usage.plan == "individual-goat"
    assert usage.subscription_status == "active"
    assert usage.allowed is None and usage.limit_reached is False
    assert usage.primary.used_percent == 0
    assert usage.primary.window_seconds == 18000
    assert usage.secondary.window_seconds == 604800
    assert usage.primary.reset_at is None and usage.primary.reset_after_seconds is None
    details = usage.command_goat
    assert details.credits.monthly_remaining == 65.6947884845
    assert details.credits.purchased_remaining == 0
    assert details.subscription.cancel_at_period_end is False
    assert details.subscription.current_period_end == "2026-11-16T00:13:24.000Z"
    assert details.window_limited is True
    assert details.five_hour.cap == 14 and details.weekly.cap == 35
    assert details.summary.total_monthly_credits == 4.3052115155
    assert details.summary.total_count == 292
    assert details.summary.period_basis == "billing-period"


@pytest.mark.parametrize("reset", [None, 0, "missing", (NOW + 300.75) * 1000])
def test_reset_times_are_optional_or_milliseconds(reset):
    payloads = _payloads()
    window = payloads[CREDITS]["windowLimits"]["fiveHour"]
    if reset == "missing":
        window.pop("resetAt")
    else:
        window["resetAt"] = reset
    usage = _parse(payloads)
    if isinstance(reset, float) and reset > 0:
        assert usage.primary.reset_at == NOW + 300.75
        assert usage.primary.reset_after_seconds == 301
        assert usage.command_goat.five_hour.reset_at == NOW + 300.75
    else:
        assert usage.primary.reset_at is None
        assert usage.primary.reset_after_seconds is None


@pytest.mark.parametrize("cap", [0, -1])
def test_nonpositive_caps_have_no_percentage(cap):
    payloads = _payloads()
    payloads[CREDITS]["windowLimits"]["fiveHour"]["cap"] = cap
    usage = _parse(payloads)
    assert usage.primary.used_percent is None
    assert usage.limit_reached is False


def test_percentage_uses_actual_cap_and_exceeded_uses_explicit_flag():
    payloads = _payloads()
    payloads[CREDITS]["windowLimits"]["fiveHour"].update(used=5, cap=10, exceeded=False)
    payloads[CREDITS]["windowLimits"]["exceeded"] = {"future-format": "ignored"}
    usage = _parse(payloads)
    assert usage.primary.used_percent == 50
    assert usage.limit_reached is False
    payloads[CREDITS]["windowLimits"]["fiveHour"].update(used=10, exceeded=False)
    assert _parse(payloads).limit_reached is False
    payloads[CREDITS]["windowLimits"]["weekly"]["exceeded"] = True
    assert _parse(payloads).limit_reached is True


@pytest.mark.parametrize("status", ["past_due", "canceled", "future-provider-status"])
def test_status_is_preserved_without_guessing_access(status):
    payloads = _payloads()
    payloads[SUB]["data"]["status"] = status
    usage = _parse(payloads)
    assert usage.subscription_status == status
    assert usage.command_goat.subscription.status == status
    assert usage.allowed is None


@pytest.mark.parametrize("org_id", [None, "org-test-scope"])
def test_queries_scope_auth_and_summary_period(org_id):
    payloads, requests = _payloads(), []
    payloads[WHO]["org"] = {"id": org_id} if org_id else None

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payloads[request.url.path])

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            usage = await fetch_command_goat_usage(client, _channel())
        assert usage.plan == "individual-goat"
    asyncio.run(exercise())
    assert [request.url.path for request in requests][0] == WHO
    assert {request.url.path for request in requests} == {WHO, SUB, CREDITS, SUMMARY}
    for request in requests:
        assert request.url.host == "api.commandcode.ai" and request.url.scheme == "https"
        assert request.headers["authorization"] == "Bearer provider-private-key"
        assert "x-private" not in request.headers
        if request.url.path == WHO:
            assert dict(request.url.params) == {"limits": "1"}
        else:
            assert request.url.params.get("orgId") == org_id
    assert requests[-1].url.params["since"] == payloads[SUB]["data"]["currentPeriodStart"]


def test_explicit_null_subscription_keeps_balances_and_omits_since():
    payloads, requests = _payloads(), []
    payloads[SUB]["data"] = None
    payloads[CREDITS]["credits"]["purchasedCredits"] = 20
    payloads[CREDITS]["windowLimits"] = {"limited": False, "exceeded": None}
    payloads[SUMMARY].pop("periodStart")
    payloads[SUMMARY].pop("periodBasis")

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payloads[request.url.path])

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            usage = await fetch_command_goat_usage(client, _channel())
        assert usage.subscription_status == "inactive" and usage.plan is None
        assert usage.command_goat.subscription is None
        assert usage.command_goat.credits.purchased_remaining == 20
        assert usage.command_goat.summary.period_start is None
        assert usage.command_goat.summary.period_basis is None
        assert usage.allowed is None and usage.limit_reached is None
        assert usage.primary is None and usage.secondary is None
    asyncio.run(exercise())
    assert "since" not in requests[-1].url.params


@pytest.mark.parametrize(("path", "payload"), [
    (WHO, {"success": True}),
    (WHO, {"org": None}),
    (WHO, {"success": True, "org": {}}),
    (WHO, {"success": True, "org": "org-id"}),
    (SUB, {"success": True}),
    (SUB, {"success": False, "data": None}),
    (SUB, {"data": None}),
    (SUB, {"success": True, "data": {}}),
    (SUB, {"success": True, "data": {"planId": "individual-goat"}}),
    (SUB, {"success": True, "data": {"planId": "", "status": "active"}}),
    (CREDITS, {"credits": None}),
    (CREDITS, {"credits": {"monthlyCredits": 0}}),
    (SUMMARY, {}),
])
def test_malformed_payload_is_not_an_inactive_subscription(path, payload):
    replies = _payloads()
    replies[path] = payload

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=replies[request.url.path]),
        )) as client:
            with pytest.raises(CommandGoatQuotaError):
                await fetch_command_goat_usage(client, _channel())
    asyncio.run(exercise())


@pytest.mark.parametrize("path", [WHO, SUB, CREDITS, SUMMARY])
@pytest.mark.parametrize(("failure", "expected_status"), [
    (401, 502), (403, 502), (429, 503), (500, 503), (302, 502),
    ("non-json", 502), ("false-success", 502), ("timeout", 503),
])
def test_all_query_failures_are_safe_and_redirects_are_not_followed(path, failure, expected_status):
    replies, requests = _payloads(), []

    def handler(request):
        requests.append(request)
        if request.url.path != path:
            return httpx.Response(200, json=replies[request.url.path])
        if failure == "timeout":
            raise httpx.ReadTimeout("provider-private-key")
        if failure == "non-json":
            return httpx.Response(200, text="provider-private-key")
        if failure == "false-success":
            return httpx.Response(200, json={"success": False, "error": "provider-private-key"})
        return httpx.Response(failure, json={"error": "provider-private-key"},
                              headers={"Location": "https://evil.test/"})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with pytest.raises(CommandGoatQuotaError) as exc:
                await fetch_command_goat_usage(client, _channel())
        assert exc.value.status_code == expected_status
        assert "provider-private-key" not in str(exc.value)
        assert all(request.url.host == "api.commandcode.ai" for request in requests)
    asyncio.run(exercise())


@pytest.mark.parametrize(("section", "invalid"), [
    (section, invalid)
    for section in ("balance", "used", "reset", "summary")
    for invalid in (True, "1", None, float("nan"), float("inf"))
    if section != "reset" or invalid is not None
])
def test_numeric_validation_rejects_bool_missing_and_nonfinite_values(invalid, section):
    payloads = _payloads()
    if section == "balance":
        payloads[CREDITS]["credits"]["monthlyCredits"] = invalid
    elif section == "used":
        payloads[CREDITS]["windowLimits"]["fiveHour"]["used"] = invalid
    elif section == "reset":
        payloads[CREDITS]["windowLimits"]["fiveHour"]["resetAt"] = invalid
    else:
        payloads[SUMMARY]["totalCost"] = invalid
    with pytest.raises(CommandGoatQuotaError):
        _parse(payloads)


@pytest.mark.parametrize(("field", "invalid"), [("exceeded", 1), ("cap", True), ("resetAt", -1), ("used", -1)])
def test_invalid_window_fields_are_errors(field, invalid):
    payloads = _payloads()
    payloads[CREDITS]["windowLimits"]["fiveHour"][field] = invalid
    with pytest.raises(CommandGoatQuotaError):
        _parse(payloads)


@pytest.mark.parametrize("limited", [False, True])
def test_snapshots_neither_change_existing_cooldowns_nor_disable_channel(limited):
    payloads = _payloads()
    payloads[CREDITS]["windowLimits"]["fiveHour"]["exceeded"] = limited
    payloads[CREDITS]["credits"]["purchasedCredits"] = 20
    channel, usage = _channel(), _parse(payloads)
    try:
        routing_engine.mark_unavailable("paid", channel, 300, reason="plan_limit")
        routing_engine.mark_unavailable("other", channel, 300)
        quota_refresher.apply_usage(channel, usage)
        assert usage_cache.fresh(channel.id) == usage
        for model, expected in [("paid", True), ("other", True), ("free", False)]:
            reasons = routing_engine.diagnose([channel], model, required_capabilities=set())[0]["reasons"]
            assert ("cooldown" in reasons) is expected
        assert channel.enabled is True
    finally:
        routing_engine.clear_cooldown(None, channel.id)
        usage_cache._entries.clear()


@asynccontextmanager
async def _api_client(monkeypatch, replies, requests, *, allowed_channels=None, provider_type="command"):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        for channel_id in (501, 502):
            db.add(Channel(id=channel_id, name=f"goat-{channel_id}", type=provider_type, auth_type="api_key",
                           key="provider-private-key", base_url="https://api.commandcode.ai/provider/v1",
                           enabled=True, models=["paid", "free"], model_mapping={}, extra={}))
        db.add(Token(key=KEY, name="goat-query", enabled=True, allowed_channels=allowed_channels))
        await db.commit()

    app = FastAPI()
    app.include_router(channels_api.router, prefix="/api/admin")
    app.include_router(usage_router, prefix="/v1")

    async def get_session():
        async with sessions() as db:
            yield db

    def handler(request):
        requests.append(request)
        reply = replies[request.url.path]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    app.dependency_overrides[get_db] = get_session
    client_class = httpx.AsyncClient
    monkeypatch.setattr(channels_api.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(handler), **kw,
    ))
    usage_cache._entries.clear()
    try:
        async with client_class(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions
    finally:
        usage_cache._entries.clear()
        routing_engine.clear_cooldown(None, 501)
        routing_engine.clear_cooldown(None, 502)
        await engine.dispose()


def test_admin_cache_refresh_and_gateway_permission_filtering(monkeypatch):
    replies, requests = _payloads(), []
    replies[SUB]["data"]["privateField"] = "provider-private-key"
    replies[WHO]["user"] = {"email": "private@example.test"}

    async def exercise():
        async with _api_client(monkeypatch, replies, requests, allowed_channels=[501]) as (client, sessions):
            path = "/api/admin/channels/501/oauth/usage"
            first = await client.get(path)
            assert first.status_code == 200
            assert first.json()["channel_enabled"] is True
            assert "provider-private-key" not in first.text and "private@example.test" not in first.text
            cached = await client.get(path)
            assert cached.json() == first.json() and len(requests) == 4
            replies[CREDITS]["credits"]["monthlyCredits"] = 50
            refreshed = await client.get(path + "?refresh=true")
            assert refreshed.json()["command_goat"]["credits"]["monthly_remaining"] == 50
            assert len(requests) == 8
            usage_cache.store(502, _parse())
            gateway = await client.get("/v1/usage", headers={"Authorization": f"Bearer {KEY}"})
            assert gateway.status_code == 200
            assert len(requests) == 8
            entries = gateway.json()["channels"]
            assert [entry["channel_id"] for entry in entries] == [501]
            assert entries[0]["provider"] == "command-goat"
            details = refreshed.json()["command_goat"]
            assert entries[0]["command_goat"] == {
                "window_limited": details["window_limited"],
                "five_hour": {key: details["five_hour"][key] for key in ("exceeded", "reset_at")},
                "weekly": {key: details["weekly"][key] for key in ("exceeded", "reset_at")},
            }
            assert entries[0]["limit_reached"] is None
            assert all(field not in gateway.text for field in ("monthly_remaining", "total_cost", "purchased_remaining", "cap"))
            assert entries[0]["allowed"] is None
            assert (await client.get("/v1/usage")).status_code == 401
            replies[WHO] = httpx.Response(401, json={"error": "provider-private-key"})
            failed = await client.get(path + "?refresh=true")
            assert failed.status_code == 502 and "provider-private-key" not in failed.text
            assert usage_cache.fresh(501).fetched_at == refreshed.json()["fetched_at"]
            async with sessions() as db:
                assert (await db.get(Channel, 501)).enabled is True
    asyncio.run(exercise())


@pytest.mark.parametrize(("reply", "expected"), [
    (httpx.Response(503, json={"error": "provider-private-key"}), 503),
    (httpx.ReadTimeout("provider-private-key"), 503),
    ({"success": True, "data": {}}, 502),
])
def test_admin_errors_preserve_channel_and_do_not_cache(monkeypatch, reply, expected):
    replies, requests = _payloads(), []
    replies[SUB] = reply

    async def exercise():
        async with _api_client(monkeypatch, replies, requests) as (client, sessions):
            response = await client.get("/api/admin/channels/501/oauth/usage")
            assert response.status_code == expected
            assert "provider-private-key" not in response.text
            assert usage_cache.fresh(501) is None
            async with sessions() as db:
                assert (await db.get(Channel, 501)).enabled is True
    asyncio.run(exercise())


@pytest.mark.parametrize("provider_type", ["openai", "anthropic", "zhipu"])
def test_wrong_command_provider_cannot_use_old_cache_or_report_gateway_usage(monkeypatch, provider_type):
    replies, requests = _payloads(), []

    async def exercise():
        async with _api_client(monkeypatch, replies, requests, provider_type=provider_type) as (client, sessions):
            usage_cache.store(501, _parse())
            gateway = await client.get("/v1/usage", headers={"Authorization": f"Bearer {KEY}"})
            assert gateway.status_code == 200 and gateway.json()["channels"] == []
            for query in ("", "?refresh=true"):
                response = await client.get("/api/admin/channels/501/oauth/usage" + query)
                assert response.status_code == 400
            assert requests == [] and usage_cache.fresh(501) is None
            async with sessions() as db:
                assert (await db.get(Channel, 501)).enabled is True
    asyncio.run(exercise())


def test_refresher_fetches_goat_without_changing_enabled_or_cooldowns(monkeypatch):
    replies, requests = _payloads(), []
    replies[CREDITS]["windowLimits"]["fiveHour"]["exceeded"] = True
    replies[CREDITS]["credits"]["purchasedCredits"] = 20

    async def exercise():
        async with _api_client(monkeypatch, replies, requests) as (client, sessions):
            monkeypatch.setattr(quota_refresher, "async_session_maker", sessions)
            refresher = quota_refresher.QuotaRefresher()
            await refresher._refresh(501)
            assert len(requests) == 4
            assert usage_cache.fresh(501).limit_reached is True
            async with sessions() as db:
                channel = await db.get(Channel, 501)
                assert channel.enabled is True
                for model in channel.models:
                    assert "cooldown" not in routing_engine.diagnose(
                        [channel], model, required_capabilities=set(),
                    )[0]["reasons"]
            replies[SUB]["data"] = None
            response = await client.get("/api/admin/channels/501/oauth/usage?refresh=true")
            assert response.status_code == 200
            assert response.json()["subscription_status"] == "inactive"
            assert response.json()["channel_enabled"] is True
            async with sessions() as db:
                assert (await db.get(Channel, 501)).enabled is True
    asyncio.run(exercise())


def test_unrecognized_channel_has_no_quota_provider():
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("unsupported host must not be queried"),
        )) as client:
            channel = _channel(base_url="https://other.example/v1")
            assert await quota_refresher.fetch_usage(channel, client) is None
            with pytest.raises(CommandGoatQuotaError):
                await fetch_command_goat_usage(client, channel)
    asyncio.run(exercise())


def test_command_url_query_does_not_change_selected_provider():
    from rotor.api.v1.usage import _provider

    replies = _payloads()
    channel = _channel(base_url="https://api.commandcode.ai/provider/v1?label=z.ai")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=replies[request.url.path]),
        )) as client:
            usage = await quota_refresher.fetch_usage(channel, client)
        assert usage.plan == "individual-goat"
        assert _provider(channel) == "command-goat"
    asyncio.run(exercise())


@pytest.mark.parametrize("window", ["fiveHour", "weekly"])
def test_gateway_window_limit_is_not_global_exhaustion_and_billing_is_private(monkeypatch, window):
    replies, requests = _payloads(), []
    replies[CREDITS]["windowLimits"][window].update(exceeded=True, used=9, resetAt=1_800_000_100_000)

    async def exercise():
        async with _api_client(monkeypatch, replies, requests, allowed_channels=[501]) as (client, sessions):
            admin = await client.get("/api/admin/channels/501/oauth/usage")
            assert admin.status_code == 200 and admin.json()["limit_reached"] is True
            assert admin.json()["command_goat"]["credits"]["monthly_remaining"] == 65.6947884845
            gateway = await client.get("/v1/usage", headers={"Authorization": f"Bearer {KEY}"})
            assert gateway.status_code == 200 and len(requests) == 4
            entry = gateway.json()["channels"][0]
            detail = entry["command_goat"]["five_hour" if window == "fiveHour" else "weekly"]
            assert entry["limit_reached"] is None and entry["allowed"] is None
            assert detail == {"exceeded": True, "reset_at": 1_800_000_100.0}
            assert all(secret not in gateway.text for secret in (
                "provider-private-key", "65.6947884845", "monthly_remaining", "total_cost", "purchased_remaining", "cap",
            ))
    asyncio.run(exercise())
