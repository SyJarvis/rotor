"""The Codex adapter must refresh a mid-life rejected token, not hand it back.

Production evidence: channel 14's access token was issued 10-25 and expired
11-04 03:32 without ever rotating. The resolver short-circuits when the stored
row still looks unexpired, so an upstream 401 on a live token would return the
same rejected token forever. These tests pin the retry.

Only the 401 path changes: every other error keeps its previous behaviour.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from rotor.adapters.providers.openai_codex import OpenAICodexAdapter
from rotor.core.exceptions import UpstreamProtocolError
from rotor.schemas.request import ChatCompletionRequest
from rotor.services.chatgpt_oauth import OAuthCredentials, OAuthError


def channel():
    return SimpleNamespace(
        id=14, name="openai-pro", type="openai_codex",
        protocol="openai_responses", auth_type="oauth",
        base_url="https://chatgpt.com/backend-api/codex", key="",
        extra={}, model_mapping={}, enabled=True, priority=1, weight=1,
    )


def request():
    return ChatCompletionRequest(
        model="gpt-6.1-sol",
        messages=[{"role": "user", "content": "hi"}],
    )


def status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    resp = httpx.Response(code, json={"error": {"message": "nope"}}, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


def adapter(monkeypatch, *, resolve):
    inst = OpenAICodexAdapter(channel(), http_client=None)
    monkeypatch.setattr(
        "rotor.services.chatgpt_oauth.resolve_oauth_credentials", resolve
    )
    return inst


def scripted(monkeypatch, inst, outcomes):
    """Replace the parent transport with a scripted sequence of outcomes."""
    calls = []
    queue = list(outcomes)

    async def parent_make_request(_self, req, timeout=None):
        calls.append(req)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        type(inst).__mro__[1], "make_request", parent_make_request, raising=True
    )
    return calls


def test_401_refreshes_with_the_rejected_credential(monkeypatch):
    old = OAuthCredentials("stale-token", "acct", 9e9)   # looks unexpired
    new = OAuthCredentials("fresh-token", "acct", 9e9 + 1)
    seen = []

    async def resolve(_channel, _client, *, rejected_credential=None):
        seen.append(rejected_credential)
        return old if rejected_credential is None else new

    inst = adapter(monkeypatch, resolve=resolve)
    ok = httpx.Response(200, json={})
    calls = scripted(monkeypatch, inst, [status_error(401), ok])

    result = asyncio.run(inst.make_request(request(), 5.0))

    assert result is ok
    assert len(calls) == 2, "the request must be retried once after refreshing"
    # First resolve is the normal lease read; the retry must pass the rejected
    # credential, otherwise the resolver returns the same stale token.
    assert seen[0] is None
    assert seen[1] == old
    assert inst._access_token == "fresh-token"


def test_second_401_is_not_retried_again(monkeypatch):
    old = OAuthCredentials("stale-token", "acct", 9e9)
    new = OAuthCredentials("fresh-token", "acct", 9e9 + 1)

    async def resolve(_channel, _client, *, rejected_credential=None):
        return old if rejected_credential is None else new

    inst = adapter(monkeypatch, resolve=resolve)
    calls = scripted(monkeypatch, inst, [status_error(401), status_error(401)])

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(inst.make_request(request(), 5.0))

    assert excinfo.value.response.status_code == 401
    assert len(calls) == 2, "a bounded retry must not loop"


def test_other_errors_do_not_refresh(monkeypatch):
    refreshes = []

    async def resolve(_channel, _client, *, rejected_credential=None):
        if rejected_credential is not None:
            refreshes.append(rejected_credential)
            raise AssertionError("a non-401 error must not refresh the token")
        return OAuthCredentials("live-token", "acct", 9e9)

    inst = adapter(monkeypatch, resolve=resolve)
    calls = scripted(monkeypatch, inst, [status_error(500)])

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(inst.make_request(request(), 5.0))

    assert excinfo.value.response.status_code == 500
    assert refreshes == []
    assert len(calls) == 1


def test_refresh_failure_surfaces_the_upstream_rejection(monkeypatch):
    old = OAuthCredentials("stale-token", "acct", 9e9)

    async def resolve(_channel, _client, *, rejected_credential=None):
        if rejected_credential is None:
            return old
        raise OAuthError(
            "ChatGPT authorization was rejected; sign in again",
            auth_status="reauth_required",
        )

    inst = adapter(monkeypatch, resolve=resolve)
    scripted(monkeypatch, inst, [status_error(401)])

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(inst.make_request(request(), 5.0))

    # The caller sees the upstream 401 (which drives re-auth), not the
    # secondary refresh error.
    assert excinfo.value.response.status_code == 401


def test_missing_credentials_never_reach_the_transport(monkeypatch):
    async def resolve(_channel, _client, *, rejected_credential=None):
        return OAuthCredentials("", "", 9e9)

    inst = adapter(monkeypatch, resolve=resolve)
    # No transport stub: the real parent path must fail while building
    # headers, before any HTTP call is attempted.
    with pytest.raises(UpstreamProtocolError):
        asyncio.run(inst.make_request(request(), 5.0))
