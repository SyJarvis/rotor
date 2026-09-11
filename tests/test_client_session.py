from rotor.core.client_session import (
    build_responses_prompt_cache_key,
    resolve_client_session,
)


def test_mindcode_session_header_is_normalized_and_tenant_scoped() -> None:
    context = resolve_client_session({
        "X-MindCode-Session-ID": "  mindcode-session  ",
        "session-id": "generic-session",
    })

    assert context.session_id == "mindcode-session"
    assert context.source == "mindcode-session-id"
    assert context.affinity_key(7) == "7:mindcode-session"
    assert context.affinity_key(8) == "8:mindcode-session"


def test_explicit_rotor_headers_precede_mindcode_session_header() -> None:
    for header in ("x-conversation-id", "x-rotor-session-id"):
        context = resolve_client_session({
            header: "explicit-session",
            "x-mindcode-session-id": "mindcode-session",
        })

        assert context.session_id == "explicit-session"
        assert context.source == header


def test_mindcode_requires_nonempty_dedicated_session_header() -> None:
    for headers in (
        {"User-Agent": "OpenAI/Python 2.0.0"},
        {"User-Agent": "MindCode/1.0"},
        {"x-mindcode-session-id": "  "},
    ):
        context = resolve_client_session(headers)
        assert context.session_id is None
        assert context.source is None

    fallback = resolve_client_session({
        "x-mindcode-session-id": " ",
        "session-id": "codex-session",
    })
    assert fallback.session_id == "codex-session"
    assert fallback.source == "codex-session-id"


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


def test_pi_session_signals_drive_pi_affinity_context() -> None:
    context = resolve_client_session({
        "User-Agent": "pi (1.2.3)",
        "session_id": "session-pi",
        "x-client-request-id": "request-pi",
    })

    assert context.session_id == "session-pi"
    assert context.source == "pi-session-id"


def test_pi_coding_agent_opencode_headers_are_supported() -> None:
    context = resolve_client_session({
        "x-opencode-client": "pi",
        "x-opencode-session": "session-pi",
        "session-id": "generic-codex-session",
    })

    assert context.session_id == "session-pi"
    assert context.source == "pi-opencode-session"


def test_pi_originator_marker_identifies_pi_session() -> None:
    context = resolve_client_session(
        {"session-id": "session-pi", "originator": "pi"}
    )

    assert context.session_id == "session-pi"
    assert context.source == "pi-session-id"


def test_pi_originator_metadata_identifies_pi_session() -> None:
    context = resolve_client_session(
        {"session-id": "session-pi"},
        metadata={"originator": "pi"},
    )

    assert context.session_id == "session-pi"
    assert context.source == "pi-session-id"


def test_pi_openrouter_session_header_is_stable() -> None:
    context = resolve_client_session({
        "User-Agent": "pi/1.2.3",
        "x-session-id": "session-pi",
    })

    assert context.session_id == "session-pi"
    assert context.source == "pi-session-id"


def test_pi_client_request_id_does_not_create_unstable_affinity() -> None:
    pi = resolve_client_session({
        "User-Agent": "pi/1.2.3",
        "x-client-request-id": "request-pi",
    })
    generic = resolve_client_session({"x-client-request-id": "request-generic"})

    assert pi.session_id is None
    assert pi.source is None
    assert generic.session_id is None


def test_grok_build_session_headers_drive_affinity_context() -> None:
    context = resolve_client_session({
        "User-Agent": "xai-grok-workspace/1.0",
        "x-grok-conv-id": "conversation-grok",
        "x-grok-session-id": "session-grok",
    })

    assert context.session_id == "conversation-grok"
    assert context.source == "grok-conv-id"


def test_explicit_rotor_header_wins_over_client_specific_signals() -> None:
    context = resolve_client_session({
        "x-rotor-session-id": "session-explicit",
        "User-Agent": "grok-shell",
        "x-grok-session-id": "session-grok",
    })

    assert context.session_id == "session-explicit"
    assert context.source == "x-rotor-session-id"


def test_grok_specific_headers_precede_generic_session_headers() -> None:
    context = resolve_client_session({
        "session-id": "generic-codex-session",
        "x-session-affinity": "generic-opencode-session",
        "x-grok-conv-id": "conversation-grok",
    })

    assert context.session_id == "conversation-grok"
    assert context.source == "xai-conv-id"


def test_grok_transport_marker_identifies_grok_without_generic_ua() -> None:
    context = resolve_client_session({
        "x-grok-client-identifier": "grok-shell",
        "x-grok-session-id": "session-grok",
        "User-Agent": "curl/8.0",
    })

    assert context.session_id == "session-grok"
    assert context.source == "grok-session-id"


def test_grok_token_auth_marker_identifies_grok() -> None:
    context = resolve_client_session({
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-session-id": "session-grok",
        "User-Agent": "curl/8.0",
    })

    assert context.session_id == "session-grok"
    assert context.source == "grok-session-id"


def test_generic_xai_conversation_header_is_not_grok_build() -> None:
    context = resolve_client_session({
        "x-grok-conv-id": "conversation-xai",
    })

    assert context.session_id == "conversation-xai"
    assert context.source == "xai-conv-id"


def test_complete_grok_session_headers_identify_grok_without_ua() -> None:
    context = resolve_client_session({
        "x-grok-conv-id": "conversation-grok",
        "x-grok-session-id": "session-grok",
        "User-Agent": "curl/8.0",
    })

    assert context.session_id == "conversation-grok"
    assert context.source == "grok-conv-id"


def test_grok_prompt_cache_key_uses_grok_source() -> None:
    context = resolve_client_session(
        {"x-grok-client-identifier": "grok-shell"},
        prompt_cache_key="cache-grok",
    )

    assert context.session_id == "cache-grok"
    assert context.source == "grok-prompt-cache-key"


def test_pi_prompt_cache_key_uses_pi_source() -> None:
    context = resolve_client_session(
        {"originator": "pi"},
        prompt_cache_key="cache-pi",
    )

    assert context.session_id == "cache-pi"
    assert context.source == "pi-prompt-cache-key"


def test_near_miss_grok_marker_does_not_create_grok_source() -> None:
    context = resolve_client_session({
        "x-grok-client-identifier": "grok-shell-helper",
        "User-Agent": "grok-shell-helper/1.0",
        "x-session-id": "generic-session",
    })

    assert context.session_id == "generic-session"
    assert context.source == "client-session-id"


def test_near_miss_grok_workspace_ua_does_not_create_grok_source() -> None:
    context = resolve_client_session({
        "User-Agent": "xai-grok-workspace-helper/1.0",
        "x-session-id": "generic-session",
    })

    assert context.session_id == "generic-session"
    assert context.source == "client-session-id"


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


def test_client_identity_does_not_require_a_session() -> None:
    for user_agent, client in (
        ("pi (darwin 25.4.0; arm64)", "pi"),
        ("MindCode/0.5.0", "mindcode"),
        ("codex-tui/0.153.4", "codex"),
        ("Codex Desktop/0.153.4", "codex"),
        ("claude-cli/2.1.251 (external, cli)", "claude_code"),
        ("opencode/1.18.29 ai-sdk/provider-utils/3", "opencode"),
    ):
        context = resolve_client_session({"User-Agent": user_agent})
        assert context.client_source == client
        assert context.routing_features()["client_source"] == client
        assert context.session_id is None
        assert context.affinity_key(1) is None


def test_mindcode_identity_survives_explicit_session_override() -> None:
    context = resolve_client_session({
        "User-Agent": "AsyncOpenAI/Python 3.8.0",
        "x-mindcode-session-id": "mindcode-session",
        "x-rotor-session-id": "explicit-session",
    })
    assert context.client_source == "mindcode"
    assert context.session_id == "explicit-session"
    assert context.source == "x-rotor-session-id"


def test_generic_sdk_and_near_miss_user_agents_are_not_branded_clients() -> None:
    for user_agent in (
        "AsyncOpenAI/Python 3.8.0", "OpenAI/Python 2.0.0",
        "codex-tui-helper/1", "claude-cli-helper/1", "opencode-helper/1",
        "pi-helper/1", "mindcode-helper/1",
    ):
        assert resolve_client_session({"User-Agent": user_agent}).client_source is None
