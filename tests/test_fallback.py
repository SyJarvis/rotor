import httpx

from rotor.gateway.fallback import should_fallback


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
