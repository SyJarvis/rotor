import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr

from rotor_mcp.client import RotorControlClient
from rotor_mcp.config import Settings
from rotor_mcp.errors import (
    ConfigurationError,
    ControlAPIError,
    InvalidControlAPIResponse,
    InvalidToolInput,
    RotorBackendUnavailable,
)


def _settings(**overrides: object) -> Settings:
    values = {
        "control_api_url": "http://rotor.test/api/control/v1",
        "control_api_token": SecretStr("control-secret"),
        "agent_id": "mindagent",
    }
    values.update(overrides)
    return Settings(**values)


def test_get_request_trace_forwards_auth_and_agent_context() -> None:
    async def exercise() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == (
                "/api/control/v1/requests/req-123/trace"
            )
            assert request.headers["Authorization"] == (
                "Bearer control-secret"
            )
            assert request.headers["X-Agent-Id"] == "mindagent"
            assert request.headers["X-Agent-Run-Id"] == "run-123"
            assert request.headers["X-Request-Id"].startswith("mcp_")
            return httpx.Response(
                200,
                json={
                    "schema_version": "1",
                    "request_id": "control-1",
                    "data": {
                        "request_id": "req-123",
                        "attempts": [],
                    },
                    "meta": {},
                },
            )

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            result = await client.get_request_trace(
                "req-123",
                agent_run_id="run-123",
            )
        await http_client.aclose()

        assert result["data"]["request_id"] == "req-123"

    asyncio.run(exercise())


def test_get_request_trace_rejects_invalid_id_before_http_call() -> None:
    async def exercise() -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("HTTP request must not be sent")

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            with pytest.raises(InvalidToolInput):
                await client.get_request_trace("../secret")
        await http_client.aclose()

    asyncio.run(exercise())


def test_get_request_trace_rejects_invalid_agent_run_id() -> None:
    async def exercise() -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("HTTP request must not be sent")

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            with pytest.raises(InvalidToolInput):
                await client.get_request_trace(
                    "req-123",
                    agent_run_id="run-123\r\nX-Injected: true",
                )
        await http_client.aclose()

    asyncio.run(exercise())


def test_get_request_trace_requires_control_token() -> None:
    async def exercise() -> None:
        async with RotorControlClient(
            _settings(control_api_token=None)
        ) as client:
            with pytest.raises(ConfigurationError) as captured:
                await client.get_request_trace("req-123")

        assert "control-secret" not in str(captured.value)

    asyncio.run(exercise())


def test_get_request_trace_maps_structured_control_error() -> None:
    async def exercise() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                404,
                json={
                    "error": {
                        "code": "request_trace_not_found",
                        "message": "Request trace not found",
                    }
                },
            )
        )
        http_client = httpx.AsyncClient(transport=transport)
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            with pytest.raises(ControlAPIError) as captured:
                await client.get_request_trace("req-missing")
        await http_client.aclose()

        assert captured.value.status_code == 404
        assert captured.value.code == "request_trace_not_found"
        assert captured.value.retryable is False

    asyncio.run(exercise())


def test_list_recent_failures_forwards_filters_and_validates_page() -> None:
    async def exercise() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/control/v1/failures"
            assert request.url.params["start_time"] == (
                "2026-07-29T00:00:00+00:00"
            )
            assert request.url.params["channel_id"] == "7"
            assert request.url.params["group_by"] == "channel"
            assert request.url.params["limit"] == "10"
            assert request.headers["X-Agent-Run-Id"] == "run-configured"
            return httpx.Response(
                200,
                json={
                    "schema_version": "1",
                    "request_id": "control-1",
                    "data": [
                        {
                            "group": {"channel_id": 7},
                            "count": 2,
                            "latest_at": "2026-07-29T00:00:00Z",
                            "sample_request_ids": ["req-1"],
                        }
                    ],
                    "page": {
                        "next_cursor": None,
                        "has_more": False,
                        "limit": 10,
                    },
                    "meta": {},
                },
            )

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(agent_run_id="run-configured"),
            http_client=http_client,
        ) as client:
            result = await client.list_recent_failures(
                start_time=datetime(
                    2026,
                    7,
                    29,
                    tzinfo=timezone.utc,
                ),
                channel_id=7,
                group_by="channel",
                limit=10,
            )
        await http_client.aclose()

        assert result["data"][0]["sample_request_ids"] == ["req-1"]

    asyncio.run(exercise())


def test_list_model_usage_forwards_window_and_validates_response() -> None:
    async def exercise() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/control/v1/usage/models"
            assert request.url.params["start_time"] == (
                "2026-07-29T00:00:00+00:00"
            )
            assert request.url.params["end_time"] == (
                "2026-07-30T00:00:00+00:00"
            )
            assert request.url.params["limit"] == "10"
            return httpx.Response(
                200,
                json={
                    "schema_version": "1",
                    "request_id": "control-usage-1",
                    "data": [
                        {
                            "model": "model-a",
                            "request_count": 2,
                            "ledger_count": 3,
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                            "cached_tokens": 1,
                            "reasoning_tokens": 2,
                            "input_audio_tokens": 0,
                            "output_audio_tokens": 0,
                        }
                    ],
                    "window": {
                        "start_time": "2026-07-29T00:00:00Z",
                        "end_time": "2026-07-30T00:00:00Z",
                        "end_exclusive": True,
                    },
                    "meta": {},
                },
            )

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            result = await client.list_model_usage(
                start_time=datetime(
                    2026,
                    7,
                    29,
                    tzinfo=timezone.utc,
                ),
                end_time=datetime(
                    2026,
                    7,
                    30,
                    tzinfo=timezone.utc,
                ),
                limit=10,
            )
        await http_client.aclose()

        assert result["data"][0]["total_tokens"] == 15
        assert result["window"]["end_exclusive"] is True

    asyncio.run(exercise())


def test_evaluate_session_leases_forwards_model_and_window() -> None:
    async def exercise() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == (
                "/api/control/v1/usage/session-leases"
            )
            assert request.url.params["start_time"] == (
                "2026-08-01T00:00:00+00:00"
            )
            assert request.url.params["end_time"] == (
                "2026-08-02T00:00:00+00:00"
            )
            assert request.url.params["model"] == "model-a"
            return httpx.Response(200, json=_session_evaluation_payload())

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            result = await client.evaluate_session_leases(
                start_time=datetime(2026, 8, 1, tzinfo=timezone.utc),
                end_time=datetime(2026, 8, 2, tzinfo=timezone.utc),
                model="model-a",
            )
        await http_client.aclose()

        assert result["data"]["facts_complete_for_evaluation"] is True
        assert result["data"]["costed_cohorts"][0]["channel_id"] == 7

    asyncio.run(exercise())


def _session_evaluation_payload() -> dict:
    return {
        "schema_version": "1",
        "request_id": "control-lease-1",
        "data": {
            "model": "model-a",
            "routing_decision_count": 10,
            "stable_session_decision_count": 8,
            "stable_session_coverage_rate": 0.8,
            "lease_preferred_decision_count": 6,
            "lease_applied_decision_count": 6,
            "lease_application_rate": 1.0,
            "lease_event_counts": {
                "assigned": 2,
                "renewed": 5,
                "migrated": 1,
                "expired": 0,
            },
            "continuation_count": 6,
            "migration_rate": 1 / 6,
            "fallback_migration_count": 1,
            "success_usage_ledger_count": 8,
            "provider_usage_coverage_rate": 1.0,
            "usage_v2_coverage_rate": 1.0,
            "cost_coverage_rate": 1.0,
            "prompt_tokens": 100,
            "uncached_input_tokens": 40,
            "cached_tokens": 50,
            "cache_write_tokens": 10,
            "uncached_input_rate": 0.4,
            "cache_read_rate": 0.5,
            "cache_write_rate": 0.1,
            "cost_totals_by_currency": {"USD": 0.25},
            "costed_cohorts": [{
                "channel_id": 7,
                "tariff_version": "v1",
                "tariff_period": "off_peak",
                "currency": "USD",
                "ledger_count": 8,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "uncached_input_tokens": 40,
                "cached_tokens": 50,
                "cache_write_tokens": 10,
                "cache_read_rate": 0.5,
                "cache_write_rate": 0.1,
                "input_cost": 0.1,
                "output_cost": 0.15,
                "total_cost": 0.25,
            }],
            "attempt_request_count": 10,
            "fallback_request_count": 1,
            "fallback_success_count": 1,
            "fallback_success_rate": 1.0,
            "rate_limited_attempt_count": 1,
            "server_error_attempt_count": 0,
            "facts_complete_for_evaluation": True,
            "blocking_reasons": [],
        },
        "window": {
            "start_time": "2026-08-01T00:00:00Z",
            "end_time": "2026-08-02T00:00:00Z",
            "end_exclusive": True,
        },
        "meta": {},
    }


def test_list_and_get_channels_forward_filters_and_validate_response() -> None:
    async def exercise() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/control/v1/channels":
                assert request.url.params["enabled"] == "true"
                assert request.url.params["model"] == "model-a"
                assert request.url.params["protocol"] == "openai"
                assert request.url.params["limit"] == "10"
                data: object = [{
                    "id": 7,
                    "name": "primary",
                    "type": "openai",
                    "base_url_origin": "https://api.example.com",
                    "protocol": "openai",
                    "models": ["model-a"],
                    "model_mapping": {},
                    "priority": 10,
                    "weight": 1,
                    "enabled": True,
                    "test_only": False,
                    "secret_state": {"has_key": True},
                }]
                extra = {
                    "page": {
                        "next_cursor": None,
                        "has_more": False,
                        "limit": 10,
                    },
                }
            else:
                assert request.url.path == "/api/control/v1/channels/7"
                data = {
                    "id": 7,
                    "name": "primary",
                    "type": "openai",
                    "base_url_origin": "https://api.example.com",
                    "protocol": "openai",
                    "models": ["model-a"],
                    "model_mapping": {},
                    "priority": 10,
                    "weight": 1,
                    "enabled": True,
                    "test_only": False,
                    "secret_state": {"has_key": True},
                }
                extra = {}
            return httpx.Response(
                200,
                json={
                    "schema_version": "1",
                    "request_id": "control-channel-1",
                    "data": data,
                    "meta": {},
                    **extra,
                },
            )

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            channels = await client.list_channels(
                enabled=True,
                model="model-a",
                protocol="openai",
                limit=10,
            )
            channel = await client.get_channel(7)
        await http_client.aclose()

        assert channels["data"][0]["id"] == 7
        assert channel["data"]["id"] == 7

    asyncio.run(exercise())


def test_get_channel_rejects_invalid_id_before_http_call() -> None:
    async def exercise() -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("HTTP request must not be sent")

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            with pytest.raises(InvalidToolInput):
                await client.get_channel(0)
        await http_client.aclose()

    asyncio.run(exercise())


def test_get_request_trace_maps_network_failure_without_leaking_url() -> None:
    async def exercise() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("secret-host failed", request=request)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with RotorControlClient(
            _settings(),
            http_client=http_client,
        ) as client:
            with pytest.raises(RotorBackendUnavailable) as captured:
                await client.get_request_trace("req-123")
        await http_client.aclose()

        assert "secret-host" not in str(captured.value)
        assert "control-secret" not in str(captured.value)

    asyncio.run(exercise())


def test_get_request_trace_rejects_oversized_response() -> None:
    async def exercise() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=b"x" * 1025,
            )
        )
        http_client = httpx.AsyncClient(transport=transport)
        async with RotorControlClient(
            _settings(control_api_max_response_bytes=1024),
            http_client=http_client,
        ) as client:
            with pytest.raises(InvalidControlAPIResponse):
                await client.get_request_trace("req-123")
        await http_client.aclose()

    asyncio.run(exercise())
