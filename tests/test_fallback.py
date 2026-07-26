import asyncio
from types import SimpleNamespace

import httpx
import pytest

from rotor.adapters.protocol.openai import OpenAIAdapter
from rotor.core.exceptions import format_error_message, upstream_error_payload
from rotor.database import _cancellation_safe
from rotor.gateway.accounting import AccountingService
from rotor.gateway.fallback import should_fallback
from rotor.schemas.request import ChatCompletionRequest, ChatMessage, Role


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/messages")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("provider error", request=request, response=response)


def test_fallback_accepts_unavailable_and_quota_errors() -> None:
    assert should_fallback(_status_error(401))
    assert should_fallback(_status_error(429))
    assert should_fallback(_status_error(503))


def test_fallback_rejects_bad_request_and_protocol_errors() -> None:
    assert not should_fallback(_status_error(400))
    assert not should_fallback(ValueError("bad conversion"))


def test_upstream_error_payload_preserves_body_and_redacts_secrets() -> None:
    request = httpx.Request("POST", "https://provider.example/responses")
    response = httpx.Response(
        503,
        request=request,
        headers={"x-request-id": "upstream-req-1"},
        json={
            "error": {"code": "overloaded", "message": "try later"},
            "api_key": "provider-secret",
        },
    )
    error = httpx.HTTPStatusError(
        "provider error", request=request, response=response
    )

    payload = upstream_error_payload(error)

    assert payload == {
        "status_code": 503,
        "request_ids": {"x-request-id": "upstream-req-1"},
        "body": {
            "error": {"code": "overloaded", "message": "try later"},
            "api_key": "[redacted]",
        },
    }
    assert '"code":"overloaded"' in format_error_message(error)
    assert "provider-secret" not in format_error_message(error)


def test_unread_streaming_error_body_never_breaks_error_formatting() -> None:
    request = httpx.Request("POST", "https://provider.example/responses")
    response = httpx.Response(
        503,
        request=request,
        stream=httpx.ByteStream(b'{"error":{"code":"overloaded"}}'),
    )
    error = httpx.HTTPStatusError(
        "provider error", request=request, response=response
    )

    payload = upstream_error_payload(error)

    assert payload["status_code"] == 503
    assert payload["body"] == "[upstream response body was not read]"


def test_streaming_adapter_reads_upstream_error_before_closing() -> None:
    async def exercise() -> dict:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503,
                json={"error": {"code": "overloaded", "message": "try later"}},
            )

        channel = SimpleNamespace(
            id=1,
            type="openai",
            protocol="openai",
            base_url="https://provider.example/v1",
            key="secret",
            extra={},
            model_mapping={},
        )
        request = ChatCompletionRequest(
            model="test-model",
            messages=[ChatMessage(role=Role.USER, content="hello")],
            stream=True,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAIAdapter(channel, client)
            with pytest.raises(httpx.HTTPStatusError) as captured:
                await adapter.make_request(request)
            return upstream_error_payload(captured.value)

    payload = asyncio.run(exercise())

    assert payload["body"]["error"]["code"] == "overloaded"


def test_failure_accounting_stores_sanitized_provider_response() -> None:
    class FakeDb:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

    db = FakeDb()
    token = SimpleNamespace(id=1, user_id="user-1")
    channel = SimpleNamespace(id=2, type="openai", protocol="openai_responses")
    provider_response = {
        "status_code": 503,
        "request_ids": {"x-request-id": "upstream-req-1"},
        "body": {"error": {"code": "overloaded"}},
    }

    asyncio.run(AccountingService().record_failure(
        db,
        request_id="req-1",
        conversation_id="conv-1",
        request_protocol="openai_responses",
        token=token,
        channel=channel,
        model="test-model",
        error_code="HTTPStatusError",
        error_message="upstream failed",
        latency_ms=20,
        client_ip="127.0.0.1",
        provider_response=provider_response,
    ))

    assert db.added[0].response_body == provider_response


def test_database_cleanup_finishes_before_cancellation_propagates() -> None:
    async def exercise() -> bool:
        started = asyncio.Event()
        completed = False

        async def cleanup() -> None:
            nonlocal completed
            started.set()
            await asyncio.sleep(0.01)
            completed = True

        task = asyncio.create_task(_cancellation_safe(cleanup()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return completed

    assert asyncio.run(exercise()) is True
