"""The Codex upstream partitions its prompt cache by the ``session_id`` header.

Measured against the live ChatGPT Codex endpoint: identical prefixes warmed
under one session id never hit under a different one (0%), while a stable id
reused across turns hit 98%. Rotor already resolved the client session for
routing affinity but never forwarded it, so every turn was a cold prefix.

These tests pin the fix: a stable per-conversation id must reach the upstream,
and unrelated conversations must not share one.
"""

from types import SimpleNamespace

from rotor.adapters.providers.openai_codex import OpenAICodexAdapter
from rotor.core.client_session import build_responses_session_id
from rotor.schemas.request import ChatCompletionRequest


def channel(**fields):
    return SimpleNamespace(
        **{
            "id": 1, "name": "codex", "type": "openai_codex",
            "protocol": "openai_responses", "auth_type": "oauth",
            "base_url": "https://chatgpt.com/backend-api/codex", "key": "",
            "extra": {}, "model_mapping": {}, "enabled": True,
            "priority": 1, "weight": 1, **fields,
        }
    )


def adapter(extra=None):
    inst = OpenAICodexAdapter(channel(extra=extra or {}), http_client=None)
    inst._access_token = "access"
    inst._account_id = "account"
    return inst


def request(session_id=None):
    req = ChatCompletionRequest(
        model="gpt-6.1-sol",
        messages=[{"role": "user", "content": "hi"}],
    )
    req.responses_session_id = session_id
    return req


def test_session_id_is_sent_to_upstream():
    headers = adapter().setup_request_headers(request("rotor_session_abc"))
    assert headers["session_id"] == "rotor_session_abc"


def test_no_session_id_header_without_a_resolved_session():
    headers = adapter().setup_request_headers(request(None))
    assert "session_id" not in headers


def test_build_request_headers_alone_omits_session_id():
    # Sub-resource calls (models/compact) have no conversation context.
    assert "session_id" not in adapter().build_request_headers()


def test_resolved_session_overrides_a_static_channel_default():
    static = {"headers": {"session_id": "channel-wide-default"}}
    headers = adapter(static).setup_request_headers(request("rotor_session_abc"))
    assert headers["session_id"] == "rotor_session_abc"


def test_static_channel_default_still_applies_without_a_session():
    static = {"headers": {"session_id": "channel-wide-default"}}
    headers = adapter(static).setup_request_headers(request(None))
    assert headers["session_id"] == "channel-wide-default"


def test_session_ids_are_stable_per_conversation():
    a = build_responses_session_id(token_id=7, session_id="conv-1")
    b = build_responses_session_id(token_id=7, session_id="conv-1")
    assert a == b
    assert a.startswith("rotor_session_")


def test_different_conversations_get_different_session_ids():
    a = build_responses_session_id(token_id=7, session_id="conv-1")
    b = build_responses_session_id(token_id=7, session_id="conv-2")
    assert a != b


def test_tenants_never_share_a_cache_bucket():
    a = build_responses_session_id(token_id=7, session_id="conv-1")
    b = build_responses_session_id(token_id=8, session_id="conv-1")
    assert a != b


def test_missing_session_yields_no_id():
    assert build_responses_session_id(token_id=7, session_id=None) is None
    assert build_responses_session_id(token_id=7, session_id="  ") is None
