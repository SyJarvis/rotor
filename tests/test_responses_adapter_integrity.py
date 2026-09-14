import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.responses import (
    chat_response_to_responses,
    extract_responses_usage,
    validate_responses_response,
)
from rotor.core.exceptions import UpstreamOverloaded, UpstreamProtocolError
from rotor.gateway.accounting import AccountingService
from rotor.schemas.request import ChatCompletionRequest


USAGE = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
         "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 2}}


def result(status="completed", **fields):
    return {"id": "resp-test", "object": "response", "status": status,
            "model": "provider-model", "output": [], "usage": deepcopy(USAGE), **fields}


def terminal(payload=None, event_type=None):
    payload = result() if payload is None else payload
    return {"type": event_type or f"response.{payload['status']}", "response": payload}


def sse(events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


def execute(payload=None, *, mode="chat", events=None, body=None, source=None, observed=None):
    streaming = events is not None or body is not None or source is not None
    request = ChatCompletionRequest(model="model", messages=[{"role": "user", "content": "hello"}], stream=streaming)
    if mode == "native":
        request.responses_payload = {"model": "model", "input": "hello", "stream": streaming}
    elif mode == "anthropic":
        request.anthropic_payload = {"model": "model", "max_tokens": 16, "messages": []}
    channel = SimpleNamespace(
        id=1, type="openai", protocol="openai_responses", name="responses",
        base_url="https://provider.invalid/v1", key="mock-provider-secret", extra={},
        model_mapping={"model": "provider-model"},
    )

    def upstream(http_request):
        assert http_request.url.path == "/v1/responses"
        assert json.loads(http_request.content)["model"] == "provider-model"
        if source is not None:
            return httpx.Response(200, stream=source)
        if streaming:
            return httpx.Response(200, content=body if body is not None else sse(events))
        return httpx.Response(200, json=payload)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            adapter = AdapterFactory.create_adapter(channel, client)
            response = await adapter.make_request(request)
            try:
                if not streaming:
                    return await adapter.convert_response(response, request)
                converted = observed if observed is not None else []
                async for event in adapter.stream_convert_response(response, request):
                    converted.append(event)
                return converted
            finally:
                await response.aclose()
    return asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["chat", "native", "anthropic"])
@pytest.mark.parametrize("payload", [{}, [], {"error": {"message": "failed"}}, result("failed"), result("cancelled")])
def test_nonstream_invalid_or_failed_response_cannot_be_success(mode, payload):
    with pytest.raises(UpstreamProtocolError) as captured:
        execute(payload, mode=mode)
    assert captured.value.upstream_status == 200


@pytest.mark.parametrize("status", ["completed", "queued", "in_progress"])
@pytest.mark.parametrize("fields", [{"id": ""}, {"id": None}, {"object": None}, {"object": "chat.completion"}])
def test_native_ownership_requires_response_identity(status, fields):
    with pytest.raises(UpstreamProtocolError):
        execute(result(status, **fields), mode="native")


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_native_background_ownership_remains_valid(status):
    payload = result(status, usage=None, provider_extension="preserved")
    assert execute(payload, mode="native") == payload
    with pytest.raises(UpstreamProtocolError):
        execute(payload)


@pytest.mark.parametrize("reason,finish_reason", [("max_output_tokens", "length"), ("content_filter", "content_filter")])
@pytest.mark.parametrize("stream", [False, True])
def test_incomplete_chat_finish_and_real_cache_usage_are_preserved(reason, finish_reason, stream):
    payload = result("incomplete", incomplete_details={"reason": reason}, output=[{
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "partial"}],
    }])
    if stream:
        converted = execute(events=[{"type": "response.output_text.delta", "delta": "partial"}, terminal(payload)])[-1]
    else:
        converted = execute(payload)
        assert converted["choices"][0]["message"]["content"] == "partial"
    assert converted["choices"][0]["finish_reason"] == finish_reason
    usage = AccountingService().extract_usage(converted)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens, usage.cache_write_tokens) == (12, 3, 8, 2)
    assert usage.usage_source == "provider"


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
def test_native_incomplete_event_and_response_are_not_downgraded(reason):
    payload = result("incomplete", incomplete_details={"reason": reason}, provider_extension="preserved")
    assert execute(payload, mode="native") == payload
    event = terminal(payload)
    assert execute(mode="native", events=[event]) == [event]


@pytest.mark.parametrize("reason", [None, "unknown_reason", [], {}])
@pytest.mark.parametrize("stream", [False, True])
def test_unknown_incomplete_reason_fails_chat_with_known_usage(reason, stream):
    payload = result("incomplete", incomplete_details={"reason": reason})
    with pytest.raises(UpstreamProtocolError) as captured:
        execute(events=[terminal(payload)]) if stream else execute(payload)
    assert captured.value.provider_usage == USAGE
    assert captured.value.upstream_status == 200


@pytest.mark.parametrize("stream", [False, True])
def test_missing_usage_does_not_become_provider_zero_usage(stream):
    payload = result()
    payload.pop("usage")
    converted = execute(events=[terminal(payload)])[-1] if stream else execute(payload)
    assert not converted.get("usage")
    assert AccountingService().extract_usage(converted).usage_source == "missing"


def test_actual_zero_usage_remains_a_provider_snapshot():
    payload = result(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    converted = execute(payload)
    assert converted["usage"]["prompt_tokens"] == 0
    assert AccountingService().extract_usage(converted).usage_source == "provider"


@pytest.mark.parametrize("usage", [
    {}, {"input_tokens": True, "output_tokens": 1}, {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": "12", "output_tokens": 1}, {"input_tokens": 12, "output_tokens": 1, "input_tokens_details": {"cached_tokens": False}},
])
def test_invalid_usage_is_not_fabricated_or_attached_as_known(usage):
    with pytest.raises(UpstreamProtocolError):
        execute(result(usage=usage))
    with pytest.raises(UpstreamProtocolError) as captured:
        execute(result("failed", usage=usage))
    assert not getattr(captured.value, "provider_usage", None)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_nonstream_preserves_only_verified_usage(status):
    payload = result(status, private_response_text="do not attach")
    with pytest.raises(UpstreamProtocolError) as captured:
        execute(payload, mode="native")
    assert captured.value.provider_usage == USAGE
    assert "do not attach" not in str(vars(captured.value))


def test_nonstream_overload_keeps_classification_usage_and_redacts_key():
    payload = result("failed", error={"type": "overloaded_error", "message": "overloaded mock-provider-secret"})
    with pytest.raises(UpstreamOverloaded) as captured:
        execute(payload, mode="native")
    assert captured.value.provider_usage == USAGE
    assert captured.value.upstream_status == 200
    assert "mock-provider-secret" not in str(captured.value)


@pytest.mark.parametrize("mode", ["chat", "native"])
@pytest.mark.parametrize("body", [
    "", "data: {bad json\n\n", "data: []\n\n", "data: {}\n\n", "data: [DONE]\n\n",
    sse([{"type": "response.output_text.delta", "delta": "partial"}]),
    sse([{"type": "error", "message": "failure", "code": "server_error"}]),
    sse([terminal(result("incomplete"), "response.completed")]),
])
def test_stream_invalid_or_missing_terminal_fails(mode, body):
    with pytest.raises(UpstreamProtocolError) as captured:
        execute(mode=mode, body=body)
    assert captured.value.upstream_status == 200


@pytest.mark.parametrize("mode", ["chat", "native"])
@pytest.mark.parametrize("late", ["data: {bad json\n\n", sse([{"type": "error", "message": "late failure"}]), sse([terminal()])])
def test_late_error_withholds_terminal_but_preserves_verified_usage(mode, late):
    observed = []
    with pytest.raises(UpstreamProtocolError) as captured:
        execute(mode=mode, body=sse([terminal()]) + late, observed=observed)
    assert observed == []
    assert captured.value.provider_usage == USAGE
    assert captured.value.upstream_status == 200


@pytest.mark.parametrize("tail_type", [
    "response.content_part.done",
    "response.output_item.done",
    "response.output_text.done",
    "response.function_call_arguments.done",
    "response.reasoning_summary_part.done",
])
def test_provider_lifecycle_events_after_terminal_are_ignored(tail_type):
    events = [terminal(), {"type": tail_type}]
    assert execute(mode="native", events=events) == [events[0]]


def test_content_after_terminal_remains_invalid():
    with pytest.raises(UpstreamProtocolError, match="after its terminal event"):
        execute(mode="native", events=[terminal(), {"type": "response.output_text.delta", "delta": "late"}])


def test_transport_error_after_terminal_keeps_usage_without_yielding_completion():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse([terminal()]).encode()
            raise httpx.ReadError("connection lost")
    observed = []
    with pytest.raises(httpx.ReadError) as captured:
        execute(mode="native", source=BrokenStream(), observed=observed)
    assert observed == []
    assert captured.value.provider_usage == USAGE


@pytest.mark.parametrize("mode", ["chat", "native", "anthropic"])
def test_multiline_no_space_comments_and_eof_frame_are_valid(mode):
    event = terminal()
    pretty = json.dumps(event, indent=2)
    body = ": heartbeat\n\nid: example\nevent:response.completed\n" + "\n".join(f"data:{line}" for line in pretty.splitlines())
    events = execute(mode=mode, body=body)
    assert len(events) == 1
    if mode == "native":
        assert events[0] == event
    else:
        assert events[0]["choices"][0]["finish_reason"] == "stop"


def test_conflicting_sse_event_name_cannot_fabricate_completion():
    with pytest.raises(UpstreamProtocolError, match="conflicts"):
        execute(mode="native", body="event: error\n" + sse([terminal()]))


@pytest.mark.parametrize("stream", [False, True])
def test_anthropic_incomplete_remains_explicitly_unsupported(stream):
    payload = result("incomplete", incomplete_details={"reason": "max_output_tokens"})
    with pytest.raises(UpstreamProtocolError):
        execute(mode="anthropic", events=[terminal(payload)]) if stream else execute(payload, mode="anthropic")


def test_public_usage_extractor_excludes_unrelated_payload_and_usage_fields():
    payload = result(private_text="not usage", usage={**USAGE, "unrelated_text": "not a counter"})
    assert extract_responses_usage(payload) == USAGE
    assert validate_responses_response(payload) is payload


def test_nullable_chat_token_details_convert_and_validate_without_fabricating_counts():
    chat = {"id": "chatcmpl-test", "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                      "prompt_tokens_details": {"cached_tokens": None},
                      "completion_tokens_details": {"reasoning_tokens": None, "audio_tokens": 2}}}
    converted = chat_response_to_responses(chat)
    assert validate_responses_response(converted) is converted
    usage = extract_responses_usage(converted)
    assert usage["input_tokens_details"] == {}
    assert usage["output_tokens_details"] == {"audio_tokens": 2}
    assert converted["usage"]["input_tokens_details"]["cached_tokens"] is None


@pytest.mark.parametrize("fields", [{}, {"usage": None}, {"usage": {}}])
def test_chat_to_responses_missing_usage_stays_missing(fields):
    converted = chat_response_to_responses({
        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}], **fields,
    })
    assert validate_responses_response(converted) is converted
    assert converted["usage"] is None
    assert extract_responses_usage(converted) is None


def test_chat_to_responses_explicit_zero_usage_stays_provider_usage():
    converted = chat_response_to_responses({
        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })
    assert validate_responses_response(converted) is converted
    assert extract_responses_usage(converted)["input_tokens"] == 0
    assert AccountingService().extract_usage(converted).usage_source == "provider"
