import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.openai_integrity import ChatStreamIntegrity, validate_chat_response
from rotor.core.exceptions import UpstreamOverloaded, UpstreamProtocolError
from rotor.models.channel import Channel
from rotor.schemas.request import ChatCompletionRequest


PROVIDERS = ["openai", "deepseek", "moonshot", "kimi", "minimax", "zhipu"]
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
         "prompt_tokens_details": {"cached_tokens": 2}}


def completion(*, finish="stop", **fields):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": finish}], **fields}


def chunk(*, index=0, delta=None, finish=None):
    return {"choices": [{"index": index, "delta": delta or {}, "finish_reason": finish}]}


def wire(events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events)


async def convert(provider, body, *, stream=False, responses_bound=False, content_type=None):
    sent = []

    async def upstream(request):
        sent.append(request)
        headers = {"content-type": content_type or ("text/event-stream" if stream else "application/json")}
        if not isinstance(body, (str, bytes)):
            return httpx.Response(200, json=body, headers=headers)
        return httpx.Response(200, content=body, headers=headers)

    channel = Channel(id=1, name="mock", type=provider, protocol="openai", base_url="https://mock.invalid/v1",
                      key="provider-secret", models=["model"], model_mapping={}, extra={})
    request = ChatCompletionRequest(model="model", messages=[{"role": "user", "content": "hello"}],
                                    stream=stream, responses_payload={"input": "hello"} if responses_bound else None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        adapter = AdapterFactory.create_adapter(channel, client)
        response = await adapter.make_request(request)
        try:
            if stream:
                result = [event async for event in adapter.stream_convert_response(response, request)]
            else:
                result = await adapter.convert_response(response, request)
        finally:
            await response.aclose()
    assert len(sent) == 1
    return result


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("responses_bound", [False, True])
@pytest.mark.parametrize("body", [{}, [], {"success": False, "msg": "provider-secret rejected"},
                                  {"error": {"code": 123, "message": "provider-secret rejected"}},
                                  {"content": "legacy flat body"}, '{broken provider-secret'])
def test_invalid_nonstream_provider_body_is_not_wrapped_as_success(provider, responses_bound, body):
    with pytest.raises(UpstreamProtocolError) as caught:
        asyncio.run(convert(provider, body, responses_bound=responses_bound))
    assert caught.value.upstream_status == 200
    assert "provider-secret" not in str(caught.value)
    assert not hasattr(caught.value, "provider_usage")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("finish", ["stop", "length", "content_filter", "tool_calls", "function_call"])
def test_valid_nonstream_stays_unchanged_without_fabricated_usage(provider, finish):
    body = completion(finish=finish)
    if finish == "tool_calls":
        body["choices"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{"}}]}
    result = asyncio.run(convert(provider, body))
    assert result == body
    assert "usage" not in result


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("responses_bound", [False, True])
@pytest.mark.parametrize("bad_frame", ['data:{bad json}\n\n', 'data: []\n\n', 'data: null\n\n',
                                       'data: {"error":{"message":"bad provider-secret"}}\n\n'])
def test_bad_sse_cannot_be_swallowed_between_valid_chunks(provider, responses_bound, bad_frame):
    body = wire([chunk(delta={"content": "partial"})]) + bad_frame + wire([chunk(finish="stop")])
    with pytest.raises(UpstreamProtocolError) as caught:
        asyncio.run(convert(provider, body, stream=True, responses_bound=responses_bound))
    assert caught.value.upstream_status == 200
    assert "provider-secret" not in str(caught.value)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_sse_multiline_no_space_comments_and_usage_tail(provider):
    first = chunk(delta={"content": "ok"})
    body = ": heartbeat\nevent: message\n" + "\n".join("data:" + line for line in json.dumps(first, indent=2).splitlines())
    body += "\n\n" + wire([chunk(finish="length"), {"choices": [], "usage": USAGE}]) + "data:[DONE]\n\n"
    result = asyncio.run(convert(provider, body, stream=True))
    assert result == [first, chunk(finish="length"), {"choices": [], "usage": USAGE}]
    state = ChatStreamIntegrity(expected_choices=1, upstream_status=200)
    for event in result:
        state.feed(event)
    state.finish()
    assert state.provider_usage == USAGE


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("body", ["", "data: [DONE]\n\n", wire([chunk(finish="stop")])])
def test_parser_leaves_empty_retry_and_finish_only_policy_to_handler(provider, body):
    result = asyncio.run(convert(provider, body, stream=True))
    assert result == ([chunk(finish="stop")] if "finish_reason" in body else [])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_json_error_body_in_stream_request_preserves_status_usage_and_redacts(provider):
    with pytest.raises(UpstreamProtocolError) as caught:
        asyncio.run(convert(provider, {"error": {"message": "bad provider-secret"}, "usage": USAGE},
                            stream=True, content_type="application/json"))
    assert caught.value.upstream_status == 200
    assert caught.value.provider_usage == USAGE
    assert "provider-secret" not in str(caught.value)


@pytest.mark.parametrize("body", [
    {"error": {"type": "overloaded_error", "message": "busy provider-secret"}},
    {"error": {"code": "rate_limit_error", "message": "busy provider-secret"}},
    {"type": "error", "message": "overloaded provider-secret"},
    {"success": False, "code": "rate_limit", "message": "busy provider-secret"},
])
@pytest.mark.parametrize("stream", [False, True])
def test_business_overload_retains_fallback_type_and_numeric_usage(body, stream):
    body = {**body, "usage": USAGE}
    with pytest.raises(UpstreamOverloaded) as caught:
        asyncio.run(convert("kimi", wire([body]) if stream else body, stream=stream))
    assert caught.value.upstream_status == 200
    assert caught.value.provider_usage == USAGE
    assert "provider-secret" not in str(caught.value)


@pytest.mark.parametrize("nested", [False, True])
def test_named_error_event_keeps_usage_even_when_frame_is_never_yielded(nested):
    error = {"type": "invalid_request_error", "message": "bad provider-secret"}
    body = {"error": error, "usage": USAGE} if nested else {**error, "usage": USAGE}
    with pytest.raises(UpstreamProtocolError) as caught:
        asyncio.run(convert("minimax", "event: error\n" + wire([body]), stream=True))
    assert caught.value.provider_usage == USAGE
    assert caught.value.upstream_status == 200
    assert "provider-secret" not in str(caught.value)


@pytest.mark.parametrize("usage", [{"prompt_tokens": -1}, {"prompt_tokens": True}, {"prompt_tokens": "7"},
                                   {"prompt_tokens": 7, "prompt_tokens_details": {"cached_tokens": "2"}},
                                   {"unknown": "provider-secret"}, {}])
def test_invalid_usage_is_not_promoted_to_provider_evidence(usage):
    with pytest.raises(UpstreamProtocolError) as caught:
        validate_chat_response({"error": {"message": "bad"}, "usage": usage}, upstream_status=200)
    assert not hasattr(caught.value, "provider_usage")


@pytest.mark.parametrize("usage", [{"prompt_tokens": -1}, {"completion_tokens": True}, {"total_tokens": "7"},
                                   {"prompt_tokens": 7, "prompt_tokens_details": {"cached_tokens": "2"}},
                                   {"completion_tokens_details": {"reasoning_tokens": -1}},
                                   {"input_tokens_details": []}, "bad", []])
def test_invalid_numeric_usage_cannot_reach_success_accounting(usage):
    with pytest.raises(UpstreamProtocolError, match="token usage") as nonstream:
        validate_chat_response(completion(usage=usage), upstream_status=200)
    assert nonstream.value.upstream_status == 200
    state = ChatStreamIntegrity(upstream_status=200)
    with pytest.raises(UpstreamProtocolError, match="token usage") as stream:
        state.feed({**chunk(finish="stop"), "usage": usage})
    assert stream.value.upstream_status == 200


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 0}, {"completion_tokens": 3},
                                   {"prompt_tokens": 0, "completion_tokens_details": {"reasoning_tokens": 0}}])
def test_missing_zero_or_partial_usage_remains_compatible(usage):
    body = completion(usage=usage)
    assert validate_chat_response(body) is body
    state = ChatStreamIntegrity()
    state.feed({**chunk(finish="stop"), "usage": usage})
    state.finish()
    assert state.provider_usage == (usage or None)


@pytest.mark.parametrize("details", [None, {"cached_tokens": None, "audio_tokens": 0},
                                     {"reasoning_tokens": None, "accepted_prediction_tokens": 2}])
@pytest.mark.parametrize("field", ["prompt_tokens_details", "completion_tokens_details"])
def test_nullable_sdk_usage_details_are_preserved_but_normalized_for_error_evidence(field, details):
    usage = {"prompt_tokens": 0, "completion_tokens": 2, field: details}
    body = completion(usage=usage)
    assert validate_chat_response(body) is body
    assert body["usage"][field] == details
    state = ChatStreamIntegrity()
    state.feed({**chunk(finish="stop"), "usage": usage})
    state.finish()
    normalized = {"prompt_tokens": 0, "completion_tokens": 2}
    if details is not None:
        normalized[field] = {key: value for key, value in details.items() if value is not None}
    assert state.provider_usage == normalized
    with pytest.raises(UpstreamProtocolError) as caught:
        validate_chat_response({"error": {"message": "bad"}, "usage": usage})
    assert caught.value.provider_usage == normalized


def test_nonstream_checks_all_choices_and_allows_minimal_compatible_fields():
    body = completion(finish="length")
    body["choices"].append({"index": 1, "message": {"content": "second"}, "finish_reason": "content_filter"})
    assert validate_chat_response(body, expected_choices=2) is body
    body["choices"][1]["finish_reason"] = None
    with pytest.raises(UpstreamProtocolError, match="finish reason"):
        validate_chat_response(body, expected_choices=2)


@pytest.mark.parametrize("choices", [[], None, [{}], [{"message": "bad", "finish_reason": "stop"}],
                                    [{"message": {}, "finish_reason": ""}],
                                    [{"message": {}, "finish_reason": " "}],
                                    [{"message": {}, "finish_reason": 0}]])
def test_nonstream_rejects_missing_message_or_real_finish(choices):
    with pytest.raises(UpstreamProtocolError) as caught:
        validate_chat_response({"choices": choices, "usage": USAGE}, upstream_status=200)
    assert caught.value.provider_usage == USAGE
    assert caught.value.upstream_status == 200


@pytest.mark.parametrize("message", [{}, {"role": "assistant"}, {"role": "user", "content": "wrong role"}])
def test_nonstream_rejects_empty_message_or_explicit_nonassistant_role(message):
    with pytest.raises(UpstreamProtocolError, match="assistant message"):
        validate_chat_response({"choices": [{"message": message, "finish_reason": "stop"}]})


@pytest.mark.parametrize("finish", ["error", "unknown", "max_tokens", "end_turn", "", " "])
def test_unknown_or_empty_finish_never_becomes_chat_or_responses_success(finish):
    with pytest.raises(UpstreamProtocolError, match="finish reason"):
        validate_chat_response(completion(finish=finish))
    state = ChatStreamIntegrity()
    with pytest.raises(UpstreamProtocolError, match="finish reason"):
        state.feed(chunk(delta={"content": "partial"}, finish=finish))


def test_stream_all_observed_choices_must_finish_and_expected_count_is_checked():
    state = ChatStreamIntegrity(expected_choices=2, upstream_status=200)
    state.feed(chunk(index=0, delta={"content": "first"}, finish="stop"))
    with pytest.raises(UpstreamProtocolError, match="choice count"):
        state.finish()
    state.feed(chunk(index=1, delta={"content": "second"}))
    with pytest.raises(UpstreamProtocolError, match="finish reasons"):
        state.finish()
    state.feed(chunk(index=1, finish="content_filter"))
    state.feed({"choices": [], "usage": USAGE})
    state.finish()


@pytest.mark.parametrize("events", [[], [chunk(delta={"content": "partial"})],
                                    [chunk(finish="stop"), chunk(index=1, delta={"content": "partial"})],
                                    [{"choices": [], "usage": USAGE}]])
def test_stream_eof_without_all_finish_reasons_is_not_success(events):
    state = ChatStreamIntegrity(upstream_status=200)
    for event in events:
        state.feed(event)
    with pytest.raises(UpstreamProtocolError, match="finish reasons") as caught:
        state.finish()
    assert caught.value.upstream_status == 200


def test_tool_arguments_are_client_validated_and_no_usage_is_fabricated():
    state = ChatStreamIntegrity(expected_choices=1)
    state.feed(chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}, finish="tool_calls"))
    state.finish()
    assert state.provider_usage is None


@pytest.mark.parametrize("bad", [chunk(finish=""), chunk(finish=123), {"choices": [False]},
                                {"choices": [{"index": True, "delta": {}}]}, {"choices": "bad"},
                                {"choices": [{"delta": "bad"}]},
                                {"choices": [{"delta": {}}, {"delta": {}}]},
                                {"choices": [{"index": 0, "delta": {}}, {"index": 0, "delta": {}}]}])
def test_stream_structural_errors_retain_observed_numeric_usage(bad):
    state = ChatStreamIntegrity(upstream_status=200)
    state.feed({"choices": [], "usage": USAGE})
    with pytest.raises(UpstreamProtocolError) as caught:
        state.feed(bad)
    assert caught.value.provider_usage == USAGE


def test_stream_error_keeps_snapshot_and_does_not_alias_usage():
    usage = deepcopy(USAGE)
    state = ChatStreamIntegrity(upstream_status=200, secret="provider-secret")
    state.feed({"choices": [], "usage": usage})
    usage["prompt_tokens"] = 999
    with pytest.raises(UpstreamProtocolError) as caught:
        state.feed({"error": {"message": "bad provider-secret"}})
    assert caught.value.provider_usage == USAGE
    assert "provider-secret" not in str(caught.value)


@pytest.mark.parametrize("late", [chunk(delta={"content": "late"}), chunk(finish="length")])
def test_stream_content_or_conflicting_finish_after_terminal_is_invalid(late):
    state = ChatStreamIntegrity()
    state.feed(chunk(finish="stop"))
    with pytest.raises(UpstreamProtocolError):
        state.feed(late)


def test_data_after_done_is_invalid_even_if_choices_finished():
    body = wire([chunk(finish="stop")]) + "data: [DONE]\n\n" + wire([chunk(finish="stop")])
    with pytest.raises(UpstreamProtocolError, match=r"after \[DONE\]"):
        asyncio.run(convert("zhipu", body, stream=True))


def test_named_error_cannot_be_disguised_as_done():
    body = wire([chunk(delta={"content": "ok"}, finish="stop")]) + "event: error\ndata: [DONE]\n\n"
    with pytest.raises(UpstreamProtocolError, match="error event"):
        asyncio.run(convert("kimi", body, stream=True))
