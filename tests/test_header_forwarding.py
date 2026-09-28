"""Channel-opt-in inbound header forwarding (``extra.forward_headers``)."""

import asyncio
from types import SimpleNamespace

from httpx import AsyncClient, MockTransport, Request, Response

from rotor.adapters.factory import AdapterFactory
from rotor.core.header_forwarding import (
    forwarded_header_names,
    select_forwarded_headers,
    snapshot_inbound_headers,
)
from rotor.schemas.request import ChatCompletionRequest


def _chat_request(**overrides) -> ChatCompletionRequest:
    payload = {
        "model": "public-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
    payload.update(overrides)
    return ChatCompletionRequest(**payload)


def _channel(extra: dict) -> SimpleNamespace:
    return SimpleNamespace(
        type="openai",
        protocol="openai",
        base_url="https://example.com/v1",
        key="provider-key",
        model_mapping={"public-model": "provider-model"},
        extra=extra,
    )


def test_snapshot_lowercases_and_strips_denylist() -> None:
    snapshot = snapshot_inbound_headers({
        "Session-Id": "abc-123",
        "Authorization": "Bearer client-token",
        "Cookie": "session=secret",
        "X-API-Key": "secret",
        "Content-Type": "application/json",
    })
    assert snapshot == {"session-id": "abc-123"}


def test_snapshot_drops_unsafe_values() -> None:
    snapshot = snapshot_inbound_headers({
        "session-id": "ok-value",
        "injected": "evil\r\nX-Injected: 1",
        "control": "be\x00ll",
        "empty": "",
        "oversized": "x" * 1025,
    })
    assert snapshot == {"session-id": "ok-value"}


def test_forwarded_header_names_validates_configuration() -> None:
    assert forwarded_header_names(None) == ()
    assert forwarded_header_names({}) == ()
    assert forwarded_header_names({"forward_headers": "session-id"}) == ()
    assert forwarded_header_names({"forward_headers": [42, None]}) == ()
    # Denylisted names can never be opted in.
    assert forwarded_header_names({"forward_headers": ["authorization"]}) == ()
    # Normalized, deduplicated.
    assert forwarded_header_names(
        {"forward_headers": ["Session-Id", "session-id", " thread-id "]}
    ) == ("session-id", "thread-id")
    # Capped at 16 entries.
    many = [f"x-h-{i}" for i in range(20)]
    assert len(forwarded_header_names({"forward_headers": many})) == 16


def test_select_returns_whitelisted_subset() -> None:
    inbound = snapshot_inbound_headers({
        "session-id": "abc",
        "thread-id": "def",
        "originator": "codex_exec",
    })
    assert select_forwarded_headers({"forward_headers": ["session-id"]}, inbound) == {
        "session-id": "abc"
    }
    # Unsafe values are re-validated at selection time.
    assert select_forwarded_headers(
        {"forward_headers": ["session-id"]}, {"session-id": "bad\r\nvalue"}
    ) == {}
    assert select_forwarded_headers({"forward_headers": ["session-id"]}, None) == {}
    assert select_forwarded_headers({}, inbound) == {}


def _run_adapter(channel_extra: dict, inbound: dict) -> Request:
    seen: list[Request] = []

    async def handler(request: Request) -> Response:
        seen.append(request)
        return Response(200, json={"choices": []})

    request = _chat_request()
    request.inbound_headers = inbound

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            adapter = AdapterFactory.create_adapter(_channel(channel_extra), client)
            response = await adapter.make_request(request)
            await response.aread()
            await response.aclose()

    asyncio.run(exercise())
    assert len(seen) == 1
    return seen[0]


def test_adapter_forwards_whitelisted_inbound_headers() -> None:
    sent = _run_adapter(
        {"forward_headers": ["session-id", "thread-id"]},
        {"session-id": "abc-123", "thread-id": "def-456", "originator": "codex_exec"},
    )
    assert sent.headers["session-id"] == "abc-123"
    assert sent.headers["thread-id"] == "def-456"
    # Not whitelisted: must not reach the provider.
    assert "originator" not in sent.headers
    # Channel authentication is untouched.
    assert sent.headers["authorization"] == "Bearer provider-key"


def test_adapter_channel_headers_win_over_forwarded() -> None:
    sent = _run_adapter(
        {
            "forward_headers": ["session-id"],
            "headers": {"Session-Id": "channel-static", "x-trace": "chan"},
        },
        {"session-id": "client-value"},
    )
    assert sent.headers["session-id"] == "channel-static"
    assert sent.headers["x-trace"] == "chan"


def test_adapter_without_configuration_forwards_nothing() -> None:
    sent = _run_adapter({}, {"session-id": "abc-123", "originator": "codex_exec"})
    assert "session-id" not in sent.headers
    assert "originator" not in sent.headers


def test_native_responses_headers_require_channel_allowlist_and_keep_channel_values() -> None:
    seen: list[Request] = []

    async def handler(request: Request) -> Response:
        seen.append(request)
        return Response(200, json={})

    request = _chat_request()
    request.responses_payload = {"model": "public-model", "input": "hello"}
    request.inbound_headers = snapshot_inbound_headers({
        "originator": "codex_exec",
        "session_id": "client-session",
        "Authorization": "Bearer client-secret",
        "X-API-Key": "client-api-key",
    })

    async def exercise() -> None:
        async with AsyncClient(transport=MockTransport(handler)) as client:
            channel = _channel({
                "auth_type": "x-api-key",
                "forward_headers": [
                    "originator", "session_id", "authorization", "x-api-key",
                ],
                "headers": {"Session-Id": "channel-static"},
            })
            channel.protocol = "openai_responses"
            adapter = AdapterFactory.create_adapter(channel, client)
            response = await adapter.make_request(request)
            await response.aread()
            await response.aclose()

    asyncio.run(exercise())
    assert len(seen) == 1
    headers = seen[0].headers
    assert headers["originator"] == "codex_exec"
    assert headers["session-id"] == "channel-static"
    assert headers["x-api-key"] == "provider-key"
