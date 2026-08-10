import asyncio
from typing import Any

import pytest
from mcp import Client

import rotor_mcp.server as server_module
from rotor_mcp.errors import InvalidControlAPIResponse
from rotor_mcp.schemas import ChannelResponse, RequestTraceResponse


class FakeControlClient:
    async def __aenter__(self) -> "FakeControlClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_request_trace(
        self,
        request_id: str,
        *,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "request_id": "control-1",
            "data": {
                "request_id": request_id,
                "agent_run_id": agent_run_id,
                "attempts": [
                    {
                        "id": "attempt-1",
                        "attempt_index": 0,
                        "channel_id": 1,
                        "outcome": "failed",
                        "error": {
                            "category": "upstream_availability",
                            "sanitized_body": {
                                "secret": "must-not-reach-agent",
                            },
                        },
                        "started_at": "2026-07-29T00:00:00Z",
                    }
                ],
            },
            "meta": {},
        }

    async def list_recent_failures(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "request_id": "control-2",
            "data": [
                {
                    "group": {"category": kwargs["group_by"]},
                    "count": 1,
                    "latest_at": "2026-07-29T00:00:00Z",
                    "sample_request_ids": ["req-123"],
                }
            ],
            "page": {
                "next_cursor": None,
                "has_more": False,
                "limit": kwargs["limit"],
            },
            "meta": {},
        }

    async def list_model_usage(
        self,
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "request_id": "control-usage-1",
            "data": [
                {
                    "model": "model-a",
                    "request_count": 2,
                    "ledger_count": 2,
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
        }

    async def list_channels(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "request_id": "control-channels-1",
            "data": [_channel_payload()],
            "page": {
                "next_cursor": None,
                "has_more": False,
                "limit": kwargs["limit"],
            },
            "meta": {},
        }

    async def get_channel(self, channel_id: int) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "request_id": "control-channel-1",
            "data": {
                **_channel_payload(),
                "id": channel_id,
            },
            "meta": {},
        }


def _channel_payload() -> dict[str, Any]:
    return {
        "id": 7,
        "name": "primary",
        "type": "openai",
        "base_url_origin": "https://api.example.com",
        "protocol": "openai",
        "models": ["model-a"],
        "model_mapping": {"model-a": "provider-model-a"},
        "priority": 10,
        "weight": 1,
        "enabled": True,
        "test_only": False,
        "secret_state": {"has_key": True},
        "updated_at": "2026-07-30T00:00:00Z",
    }


def test_mcp_server_exposes_and_calls_request_trace_tool(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        monkeypatch.setattr(
            server_module,
            "create_control_client",
            FakeControlClient,
        )

        async with Client(server_module.server) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "rotor_get_request_trace",
                {
                    "request_id": "req-123",
                },
            )
            failures = await client.call_tool(
                "rotor_list_recent_failures",
                {
                    "group_by": "category",
                    "limit": 10,
                },
            )
            usage = await client.call_tool(
                "rotor_list_model_usage",
                {
                    "start_time": "2026-07-29T00:00:00+08:00",
                    "end_time": "2026-07-30T00:00:00+08:00",
                    "limit": 10,
                },
            )
            channels = await client.call_tool(
                "rotor_list_channels",
                {
                    "enabled": True,
                    "model": "model-a",
                    "limit": 10,
                },
            )
            channel = await client.call_tool(
                "rotor_get_channel",
                {
                    "channel_id": 7,
                },
            )

        assert {tool.name for tool in tools.tools} == {
            "rotor_get_channel",
            "rotor_get_request_trace",
            "rotor_list_channels",
            "rotor_list_model_usage",
            "rotor_list_recent_failures",
        }
        trace_tool = next(
            tool
            for tool in tools.tools
            if tool.name == "rotor_get_request_trace"
        )
        assert trace_tool.input_schema["properties"]["request_id"][
            "maxLength"
        ] == 64
        assert trace_tool.output_schema["additionalProperties"] is False
        assert "data" in trace_tool.output_schema["properties"]
        assert trace_tool.annotations.read_only_hint is True
        assert trace_tool.annotations.destructive_hint is False
        assert trace_tool.annotations.idempotent_hint is True
        assert trace_tool.annotations.open_world_hint is False
        assert result.structured_content["data"]["request_id"] == "req-123"
        assert result.structured_content["data"]["agent_run_id"] is None
        error = result.structured_content["data"]["attempts"][0]["error"]
        assert "sanitized_body" not in error
        assert result.structured_content["meta"]["redactions"] == [
            "data.attempts[0].error.sanitized_body"
        ]
        assert failures.structured_content["data"][0][
            "sample_request_ids"
        ] == ["req-123"]
        failures_tool = next(
            tool
            for tool in tools.tools
            if tool.name == "rotor_list_recent_failures"
        )
        assert failures_tool.output_schema["additionalProperties"] is False
        assert failures_tool.annotations.read_only_hint is True
        usage_tool = next(
            tool
            for tool in tools.tools
            if tool.name == "rotor_list_model_usage"
        )
        assert usage_tool.output_schema["additionalProperties"] is False
        assert usage_tool.annotations.read_only_hint is True
        assert usage.structured_content["data"][0]["total_tokens"] == 15
        channels_tool = next(
            tool
            for tool in tools.tools
            if tool.name == "rotor_list_channels"
        )
        assert channels_tool.output_schema["additionalProperties"] is False
        assert channels_tool.annotations.read_only_hint is True
        assert channels.structured_content["data"][0]["id"] == 7
        channel_tool = next(
            tool
            for tool in tools.tools
            if tool.name == "rotor_get_channel"
        )
        assert channel_tool.input_schema["properties"]["channel_id"][
            "minimum"
        ] == 1
        assert channel_tool.annotations.read_only_hint is True
        assert channel.structured_content["data"]["name"] == "primary"

    asyncio.run(exercise())


def test_request_trace_schema_rejects_unknown_fields_without_leaking_input(
) -> None:
    payload = {
        "schema_version": "1",
        "request_id": "control-1",
        "data": {
            "request_id": "req-123",
            "unexpected": "sensitive-value",
        },
        "meta": {},
    }

    with pytest.raises(InvalidControlAPIResponse) as captured:
        RequestTraceResponse.from_control_payload(payload)

    assert "sensitive-value" not in str(captured.value)


def test_request_trace_schema_preserves_control_api_redactions() -> None:
    path = "data.attempts[0].error.sanitized_body"
    payload = {
        "schema_version": "1",
        "request_id": "control-1",
        "data": {
            "request_id": "req-123",
        },
        "meta": {
            "redactions": [path],
        },
    }

    response = RequestTraceResponse.from_control_payload(payload)

    assert response.meta.redactions == [path]


def test_channel_schema_rejects_sensitive_unknown_fields() -> None:
    payload = {
        "schema_version": "1",
        "request_id": "control-channel-1",
        "data": {
            **_channel_payload(),
            "key": "provider-secret",
        },
        "meta": {},
    }

    with pytest.raises(InvalidControlAPIResponse) as captured:
        ChannelResponse.from_control_payload(payload)

    assert "provider-secret" not in str(captured.value)
