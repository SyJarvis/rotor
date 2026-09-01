from rotor.core.client_session import (
    build_responses_prompt_cache_key,
    resolve_client_session,
)


def test_codex_session_headers_drive_affinity_context() -> None:
    context = resolve_client_session(
        {
            "session-id": "session-codex",
            "thread-id": "thread-codex",
        },
        prompt_cache_key="cache-codex",
    )

    assert context.session_id == "session-codex"
    assert context.thread_id == "thread-codex"
    assert context.cache_key == "cache-codex"
    assert context.source == "codex-session-id"
    assert context.affinity_key(7) == "7:session-codex"


def test_claude_code_header_wins_over_metadata_session() -> None:
    context = resolve_client_session(
        {"x-claude-code-session-id": "session-claude-header"},
        metadata={
            "user_id": (
                '{"device_id":"device","session_id":'
                '"session-claude-metadata"}'
            )
        },
    )

    assert context.session_id == "session-claude-header"
    assert context.source == "claude-code-session-id"


def test_claude_code_metadata_session_is_a_header_fallback() -> None:
    context = resolve_client_session(
        {},
        metadata={
            "user_id": (
                '{"device_id":"device","session_id":'
                '"session-claude-metadata"}'
            )
        },
    )

    assert context.session_id == "session-claude-metadata"
    assert context.source == "metadata-user-session-id"


def test_opencode_affinity_header_is_a_stable_session() -> None:
    context = resolve_client_session(
        {
            "X-Session-Affinity": "session-opencode",
            "X-Session-Id": "session-secondary",
        }
    )

    assert context.session_id == "session-opencode"
    assert context.source == "opencode-session-affinity"


def test_explicit_conversation_header_has_highest_priority() -> None:
    context = resolve_client_session(
        {
            "X-Conversation-Id": "session-explicit",
            "session-id": "session-codex",
        },
        conversation="conversation-native",
        prompt_cache_key="cache-key",
    )

    assert context.session_id == "session-explicit"
    assert context.source == "x-conversation-id"


def test_prompt_cache_key_is_used_when_no_session_hint_exists() -> None:
    context = resolve_client_session({}, prompt_cache_key="cache-session")

    assert context.session_id == "cache-session"
    assert context.source == "prompt-cache-key"


def test_long_untrusted_identifier_is_stably_bounded() -> None:
    raw_session_id = "s" * 101

    first = resolve_client_session({"session-id": raw_session_id})
    second = resolve_client_session({"session-id": raw_session_id})

    assert first.session_id == second.session_id
    assert first.session_id is not None
    assert len(first.session_id) == 68


def test_missing_session_does_not_create_fake_affinity() -> None:
    context = resolve_client_session({})

    assert context.session_id is None
    assert context.source is None
    assert context.affinity_key(7) is None


def test_responses_prompt_cache_key_is_stable_bounded_and_private() -> None:
    first = build_responses_prompt_cache_key(
        token_id=7,
        model="gpt-5.6-sol",
        session_id="private-claude-session",
    )
    second = build_responses_prompt_cache_key(
        token_id=7,
        model="gpt-5.6-sol",
        session_id="private-claude-session",
    )
    other = build_responses_prompt_cache_key(
        token_id=8,
        model="gpt-5.6-sol",
        session_id="private-claude-session",
    )

    assert first == second
    assert first != other
    assert first is not None
    assert len(first) == 64
    assert "private-claude-session" not in first
    assert build_responses_prompt_cache_key(
        token_id=7,
        model="gpt-5.6-sol",
        session_id=None,
    ) is None
