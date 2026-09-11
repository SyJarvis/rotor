import asyncio
import json

import httpx
from starlette.requests import Request

from rotor.core.anthropic_errors import protocol_upstream_exception_handler
from rotor.core.openai_errors import openai_error_payload, openai_error_response


def _request(path: str = "/v1/chat/completions") -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "client": ("127.0.0.1", 1),
    })


def _status_error(
    status_code: int,
    body: dict,
    headers: dict | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST", "https://opencode.ai/zen/go/v1/chat/completions"
    )
    response = httpx.Response(
        status_code,
        request=request,
        headers=headers or {},
        json=body,
    )
    return httpx.HTTPStatusError(
        f"upstream returned {status_code}",
        request=request,
        response=response,
    )


def test_openai_error_payload_keeps_upstream_status_and_sanitized_body():
    error = _status_error(
        400,
        {"error": {"message": "unknown model", "api_key": "provider-secret"}},
    )

    payload = openai_error_payload(error)

    assert payload["error"]["message"] == "Upstream returned HTTP 400"
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["code"] == "upstream_protocol_or_parameter_error"
    assert payload["error"]["upstream"]["status_code"] == 400
    assert payload["error"]["upstream"]["body"]["error"]["message"] == "unknown model"
    assert "provider-secret" not in json.dumps(payload)


def test_openai_error_response_preserves_status_and_retry_after():
    error = _status_error(
        429,
        {"error": {"message": "slow down"}},
        {"retry-after": "7"},
    )

    response = openai_error_response(error)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert b"slow down" in response.body


def test_protocol_upstream_handler_answers_openai_clients_with_upstream_status():
    error = _status_error(400, {"error": {"message": "unsupported parameter"}})

    response = asyncio.run(
        protocol_upstream_exception_handler(_request(), error)
    )

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"]["message"] == "Upstream returned HTTP 400"
    assert (
        body["error"]["upstream"]["body"]["error"]["message"]
        == "unsupported parameter"
    )


def test_protocol_upstream_handler_keeps_anthropic_envelope():
    error = _status_error(400, {"error": {"message": "unsupported parameter"}})

    response = asyncio.run(
        protocol_upstream_exception_handler(_request("/v1/messages"), error)
    )

    assert response.status_code == 400
    assert json.loads(response.body)["type"] == "error"


def test_protocol_upstream_handler_maps_connection_failures_to_bad_gateway():
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    error = httpx.ConnectError("connection refused", request=request)

    response = asyncio.run(
        protocol_upstream_exception_handler(_request(), error)
    )

    assert response.status_code == 502
    assert json.loads(response.body)["error"]["message"] == (
        "Upstream connection failed"
    )
