import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

import rotor.api.v1.chat as chat_endpoint
import rotor.api.v1.responses as responses_endpoint
from rotor.adapters.protocol.responses import (
    chat_response_to_responses,
    responses_required_capabilities,
)
from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import RoutingEngine
from rotor.schemas.responses import ResponsesRequest
from rotor.services.session_leases import SessionLeasePreference


@pytest.fixture
def endpoint(monkeypatch):
    channels = [
        SimpleNamespace(
            id=index, name=protocol, type="openai", protocol=protocol,
            enabled=True, priority=priority, weight=1, model_mapping={},
            extra={}, base_url=f"https://upstream-{index}.invalid/v1", key="test-key",
        )
        for index, protocol, priority in [(1, "openai", 10), (2, "openai_responses", 1)]
    ]
    token = SimpleNamespace(id=1, allowed_channels=None)
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.get.return_value = token
    accounting = SimpleNamespace(
        record_routing_decision=Mock(), record_success=AsyncMock(),
        record_failure=AsyncMock(), record_lease_success=AsyncMock(),
        extract_usage=AccountingService().extract_usage,
        streaming_usage=AccountingService().streaming_usage,
    )
    conversations = AsyncMock()
    conversations.start.return_value = SimpleNamespace(conversation_id="conv-test")
    for module in (chat_endpoint, responses_endpoint):
        monkeypatch.setattr(module, "accounting_service", accounting)
        monkeypatch.setattr(module, "attempt_recorder", SimpleNamespace(record=AsyncMock()))
        monkeypatch.setattr(module, "conversation_store", conversations)
        monkeypatch.setattr(module, "async_session_maker", lambda: db)
    monkeypatch.setattr(responses_endpoint, "get_available_channels", AsyncMock(return_value=channels))
    preferred = AsyncMock(return_value=SessionLeasePreference(None))
    monkeypatch.setattr(responses_endpoint, "get_session_lease_preference", preferred)
    monkeypatch.setattr(responses_endpoint, "save_response_route", AsyncMock())
    engine = RoutingEngine("fallback_order")
    monkeypatch.setattr(responses_endpoint, "routing_engine", engine)
    settings = SimpleNamespace(affinity_enabled=True, session_lease_enabled=True, session_lease_reassess_seconds=300)
    monkeypatch.setattr(responses_endpoint, "application_settings", SimpleNamespace(get=lambda: SimpleNamespace(routing=settings)))
    sent = []
    reply = {"chunks": None, "message": {"content": "ok"}, "finish_reason": "stop"}

    def upstream(request):
        body = json.loads(request.content)
        sent.append((request.url.host, body))
        if request.url.path.endswith("/responses"):
            native = {
                "id": "resp_native", "object": "response", "model": "test-model",
                "status": "completed", "created_at": 1,
                "output": [{"type": "message", "id": "msg_native", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "native", "annotations": []}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            if body.get("stream"):
                event = {"type": "response.completed", "response": native, "sequence_number": 0}
                return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n", headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=native)
        if body.get("stream"):
            events = reply["chunks"] or []
            return httpx.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in events) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "chat_test", "model": "test-model", "created": 1,
            "choices": [{"message": {"role": "assistant", **reply["message"]}, "finish_reason": reply["finish_reason"]}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(responses_endpoint, "AsyncClient", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))

    async def call(**fields):
        request = ResponsesRequest.model_validate({"model": "test-model", "input": "hello", **fields})
        http_request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [(b"x-request-id", b"req-semantics"), (b"x-session-id", b"session-test")], "client": ("127.0.0.1", 1)})
        result = await responses_endpoint.create_response(request, http_request, Response(), db, token)
        if fields.get("stream"):
            return [json.loads(chunk.removeprefix("data: ").strip()) async for chunk in result.body_iterator]
        return result

    return SimpleNamespace(call=call, channels=channels, engine=engine, preferred=preferred, sent=sent, reply=reply, accounting=accounting)


@pytest.mark.parametrize("affinity,lease,expected", [(True, None, 2), (False, None, 1), (True, 1, 1), (False, 2, 2)])
def test_responses_endpoint_respects_engine_order(endpoint, affinity, lease, expected):
    endpoint.engine.protocol_affinity_enabled = affinity
    endpoint.preferred.return_value = SessionLeasePreference(lease)
    asyncio.run(endpoint.call())
    assert [host for host, _ in endpoint.sent] == [f"upstream-{expected}.invalid"]


@pytest.mark.parametrize("fields", [
    {"text": {"format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}}}},
    {"text": {"verbosity": "low"}},
    {"parallel_tool_calls": False}, {"truncation": "auto"},
    {"store": True}, {"max_tool_calls": 1}, {"service_tier": "priority"},
])
def test_responses_constraints_reach_native_upstream(endpoint, fields):
    endpoint.engine.protocol_affinity_enabled = False
    asyncio.run(endpoint.call(**fields))
    assert endpoint.sent[0][0] == "upstream-2.invalid"
    for key, value in fields.items():
        assert endpoint.sent[0][1][key] == value


@pytest.mark.parametrize("fields", [{"parallel_tool_calls": True}, {"text": {}}, {"text": {"format": {"type": "text"}}}, {"store": False}, {"truncation": "disabled"}, {"service_tier": "auto"}])
def test_responses_default_options_remain_convertible(fields):
    request = ResponsesRequest(model="test-model", input="hello", **fields)
    assert "responses_native" not in responses_required_capabilities(request)


def test_responses_image_excludes_channel_without_vision(endpoint):
    endpoint.engine.protocol_affinity_enabled = False
    endpoint.channels[0].extra = {"capabilities": ["stream", "function_call"]}
    endpoint.channels[1].extra = {"capabilities": ["vision"]}
    asyncio.run(endpoint.call(input=[{"role": "user", "content": [{"type": "input_image", "image_url": "https://image.invalid/a.png"}]}]))
    assert endpoint.sent[0][0] == "upstream-2.invalid"


@pytest.mark.parametrize("affinity", [False, True])
def test_optional_tool_downgrade_is_logged_only_for_actual_conversion(endpoint, caplog, affinity):
    endpoint.engine.protocol_affinity_enabled = affinity
    asyncio.run(endpoint.call(tools=[{"type": "web_search", "private_option": "do-not-log-this"}, {"type": "namespace", "name": "private-name", "tools": []}]))
    records = [record for record in caplog.records if "Responses protocol downgrade:" in record.getMessage()]
    assert len(records) == (0 if affinity else 1)
    if records:
        payload = json.loads(records[0].getMessage().split(": ", 1)[1])
        assert payload == {"event": "responses_protocol_downgrade", "request_id": "req-semantics", "channel_id": 1, "target_protocol": "openai", "omitted_tool_types": ["namespace", "web_search"], "reason": "optional_tools_have_no_chat_representation"}
        assert "do-not-log-this" not in records[0].getMessage()
        assert "private-name" not in records[0].getMessage()


@pytest.mark.parametrize("finish_reason,reason", [("length", "max_output_tokens"), ("content_filter", "content_filter")])
@pytest.mark.parametrize("stream", [False, True])
def test_truncated_responses_are_incomplete(endpoint, finish_reason, reason, stream):
    pytest.importorskip("openai")
    endpoint.engine.protocol_affinity_enabled = False
    endpoint.reply["message"] = {"content": "partial"}
    endpoint.reply["finish_reason"] = finish_reason
    endpoint.reply["chunks"] = [{"choices": [{"delta": {"content": "partial"}}]}, {"choices": [{"delta": {}, "finish_reason": finish_reason}]}]
    result = asyncio.run(endpoint.call(stream=stream))
    if stream:
        from openai.types.responses.response_incomplete_event import ResponseIncompleteEvent
        ResponseIncompleteEvent.model_validate(result[-1])
        assert result[-1]["type"] == "response.incomplete"
        result = result[-1]["response"]
    assert result["status"] == "incomplete"
    assert result["incomplete_details"] == {"reason": reason}
    assert result["output"][0]["status"] == "incomplete"
    assert len(endpoint.sent) == 1


@pytest.mark.parametrize("field,text", [("refusal", "cannot comply"), ("reasoning_content", "reasoning text")])
@pytest.mark.parametrize("stream", [False, True])
def test_nontext_responses_output_is_preserved_without_replay(endpoint, field, text, stream):
    pytest.importorskip("openai")
    endpoint.engine.protocol_affinity_enabled = False
    endpoint.reply["message"] = {field: text}
    endpoint.reply["chunks"] = [{"choices": [{"delta": {field: text}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    result = asyncio.run(endpoint.call(stream=stream))
    if stream:
        from pydantic import TypeAdapter
        from openai.types.responses.response_stream_event import ResponseStreamEvent
        event_parser = TypeAdapter(ResponseStreamEvent)
        for event in result:
            event_parser.validate_python(event)
        kind = "refusal" if field == "refusal" else "reasoning_text"
        assert [event["type"] for event in result] == [
            "response.created", "response.in_progress", "response.output_item.added",
            "response.content_part.added", f"response.{kind}.delta", f"response.{kind}.done",
            "response.content_part.done", "response.output_item.done", "response.completed",
        ]
        assert [event["sequence_number"] for event in result] == list(range(len(result)))
        assert {event["output_index"] for event in result if "output_index" in event} == {0}
        assert {event["content_index"] for event in result if "content_index" in event} == {0}
        result = result[-1]["response"]
    from openai.types.responses.response import Response as SDKResponse
    SDKResponse.model_validate(result)
    item = result["output"][0]
    if field == "refusal":
        assert item["content"] == [{"type": "refusal", "refusal": text}]
    else:
        assert item["type"] == "reasoning" and item["summary"] == []
        assert item["content"] == [{"type": "reasoning_text", "text": text}]
    assert result["status"] == "completed"
    assert len(endpoint.sent) == 1


def test_complex_reasoning_fails_explicitly_without_replay(endpoint):
    pytest.importorskip("openai")
    endpoint.engine.protocol_affinity_enabled = False
    endpoint.reply["chunks"] = [{"choices": [{"delta": {"reasoning_content": {"encrypted": "opaque"}}}]}]
    result = asyncio.run(endpoint.call(stream=True))
    assert result[-1]["type"] == "response.failed"
    assert "native Responses" in result[-1]["response"]["error"]["message"]
    assert len(endpoint.sent) == 1
    endpoint.accounting.record_success.assert_not_awaited()
    from openai.types.responses.response_failed_event import ResponseFailedEvent
    ResponseFailedEvent.model_validate(result[-1])
    nonstream = chat_response_to_responses({"model": "test-model", "id": "chat-test", "choices": [{"message": {"reasoning_content": {"encrypted": "opaque"}}}]})
    assert nonstream["status"] == "failed"
    assert nonstream["error"]["code"] == "server_error"
    from openai.types.responses.response import Response as SDKResponse
    SDKResponse.model_validate(nonstream)


def test_native_responses_stream_is_preserved(endpoint):
    result = asyncio.run(endpoint.call(stream=True, reasoning={"effort": "high"}))
    assert [event["type"] for event in result] == ["response.completed"]
    assert result[0]["response"]["id"] == "resp_native"
    assert endpoint.sent[0][1]["reasoning"] == {"effort": "high"}
    assert len(endpoint.sent) == 1
