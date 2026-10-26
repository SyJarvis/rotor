import asyncio
import base64
import io
import json
import zipfile
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.responses import StreamingResponse

import rotor.api.admin.chat as endpoint
from rotor.database import Base
from rotor.gateway.streaming import AdmissionStreamingResponse
from rotor.models.channel import Channel
from rotor.models.token import Token


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def attachment(raw=PNG, *, mime="image/png", kind="image", name="image.png"):
    return {
        "kind": kind, "name": name, "mime_type": mime, "size": len(raw),
        "data_url": f"data:{mime};base64,{base64.b64encode(raw).decode()}",
    }


def conversation(**kwargs):
    return endpoint.AdminChatRequest(**{
        "conversation_id": "chat-test", "model": "model-a",
        "messages": [{"role": "user", "content": "hello"}], **kwargs,
    })


def http_request():
    return Request({
        "type": "http", "method": "POST", "path": "/api/admin/chat/completions",
        "headers": [(b"host", b"attacker.invalid"), (b"authorization", b"Bearer private")],
        "client": ("127.0.0.1", 1234),
    })


class Sessions:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def __call__(self):
        owner = self

        class Session:
            async def __aenter__(self):
                owner.opened += 1
                return self

            async def __aexit__(self, *args):
                owner.closed += 1
        return Session()


def chat_sse(content="你好", *, finish="stop", usage=True):
    yield "data: " + json.dumps({
        "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "model-a",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }, ensure_ascii=False) + "\n\n"
    if finish:
        yield "data: " + json.dumps({
            "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "model-a",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            **({"usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}} if usage else {}),
        }) + "\n\n"
    yield "data: [DONE]\n\n"


def responses_sse(content="你好", *, terminal="completed"):
    yield "event: response.output_text.delta\ndata: " + json.dumps({
        "type": "response.output_text.delta", "sequence_number": 0,
        "item_id": "msg-1", "output_index": 0, "content_index": 0, "delta": content,
    }, ensure_ascii=False) + "\n\n"
    if terminal:
        yield f"event: response.{terminal}\ndata: " + json.dumps({
            "type": f"response.{terminal}", "sequence_number": 1,
            "response": {
                "id": "resp-1", "object": "response", "created_at": 1, "status": terminal,
                "model": "model-a", "output": [], "error": {"message": "provider failed"} if terminal == "failed" else None,
                "incomplete_details": {"reason": "max_output_tokens"} if terminal == "incomplete" else None,
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        }) + "\n\n"


@pytest.mark.parametrize("api_format", ["chat", "responses"])
def test_sdk_stream_forwards_settings_and_multiturn_attachments(monkeypatch, api_format):
    sessions = Sessions()
    monkeypatch.setattr(endpoint, "async_session_maker", sessions)
    observed = {}
    closed = []
    token = SimpleNamespace(id=7, key="do-not-forward")
    image = attachment()
    document = {"kind": "file", "name": "note.txt", "mime_type": "text/plain", "size": 5, "content": "facts"}

    async def gateway(data, request, response, db, chosen_token):
        observed["data"] = data
        observed["headers"] = dict(request.headers)
        observed["origin"] = request.state.request_origin
        assert chosen_token is token

        async def body():
            try:
                chunks = chat_sse() if api_format == "chat" else responses_sse()
                for chunk in chunks:
                    # The SDK must decode split SSE frames and UTF-8 bytes itself.
                    raw = chunk.encode()
                    for index in range(0, len(raw), 7):
                        yield raw[index:index + 7]
            finally:
                closed.append("body")
        return StreamingResponse(body())

    monkeypatch.setattr(endpoint, "chat_completions", gateway)
    monkeypatch.setattr(endpoint, "create_response", gateway)
    request = conversation(
        api_format=api_format, system_prompt="Answer precisely", temperature=0.4, max_tokens=123,
        messages=[
            {"role": "user", "content": "see these", "attachments": [image, document]},
            {"role": "assistant", "content": "understood"},
            {"role": "user", "content": "what were they?"},
        ],
    )

    async def run():
        return [event async for event in endpoint._completion_events(request, http_request(), token)]
    events = asyncio.run(run())
    assert events == [
        {"type": "delta", "content": "你好"},
        {"type": "done", "answer": "你好", "usage": (
            {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            if api_format == "chat" else {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
        )},
    ]
    data = observed["data"]
    assert data.temperature == 0.4
    if api_format == "chat":
        assert data.max_tokens == 123
        assert data.messages[0].role.value == "system"
        assert data.messages[0].content == "Answer precisely"
        assert data.messages[1].content[1]["image_url"]["url"] == image["data_url"]
        assert "facts" in data.messages[1].content[2]["text"]
        assert data.messages[-1].content == "what were they?"
    else:
        assert data.max_output_tokens == 123
        assert data.instructions == "Answer precisely"
        assert data.input[0]["content"][1]["image_url"] == image["data_url"]
        assert "facts" in data.input[0]["content"][2]["text"]
        assert data.input[-1]["content"] == "what were they?"
    assert observed["headers"] == {"x-conversation-id": "chat-test"}
    assert observed["origin"] == "admin_chat"
    assert closed == ["body"]
    assert sessions.opened == sessions.closed == 1


@pytest.mark.parametrize("api_format", ["chat", "responses"])
def test_null_settings_are_omitted_in_sdk_body(monkeypatch, api_format):
    captured = []
    original = endpoint._GatewayTransport.handle_async_request

    async def capture(self, request):
        captured.append(json.loads(await request.aread()))
        return await original(self, request)

    async def gateway(data, *args):
        assert data.temperature is None
        if api_format == "chat":
            from rotor.adapters.protocol.openai import OpenAIAdapter
            channel = SimpleNamespace(model_mapping={})
            adapter = OpenAIAdapter(channel, None)
            body = await adapter.convert_request(data)
            assert not {"temperature", "top_p", "n", "presence_penalty", "frequency_penalty"} & body.keys()
        async def body():
            for item in (chat_sse() if api_format == "chat" else responses_sse()):
                yield item
        return StreamingResponse(body())

    monkeypatch.setattr(endpoint, "async_session_maker", Sessions())
    monkeypatch.setattr(endpoint._GatewayTransport, "handle_async_request", capture)
    monkeypatch.setattr(endpoint, "chat_completions", gateway)
    monkeypatch.setattr(endpoint, "create_response", gateway)

    async def run():
        return [event async for event in endpoint._completion_events(
            conversation(api_format=api_format), http_request(), SimpleNamespace(id=1)
        )]
    asyncio.run(run())
    assert "temperature" not in captured[0]
    assert "max_tokens" not in captured[0]
    assert "max_output_tokens" not in captured[0]


@pytest.mark.parametrize("api_format,terminal,content", [
    ("chat", "stop", ""), ("chat", None, "partial"), ("chat", "content_filter", "partial"),
    ("responses", "failed", "partial"), ("responses", "incomplete", "partial"),
    ("responses", None, "partial"), ("responses", "completed", ""),
])
def test_empty_incomplete_and_failed_streams_never_report_done(monkeypatch, api_format, terminal, content):
    sessions = Sessions()
    monkeypatch.setattr(endpoint, "async_session_maker", sessions)

    async def gateway(*args):
        async def body():
            chunks = chat_sse(content, finish=terminal) if api_format == "chat" else responses_sse(content, terminal=terminal)
            for chunk in chunks:
                yield chunk
        return StreamingResponse(body())
    monkeypatch.setattr(endpoint, "chat_completions", gateway)
    monkeypatch.setattr(endpoint, "create_response", gateway)

    async def run():
        return [json.loads(event[6:]) async for event in endpoint._safe_chat_stream(
            conversation(api_format=api_format), http_request(), SimpleNamespace(id=1)
        )]
    events = asyncio.run(run())
    assert events[-1]["type"] == "error"
    assert not any(event["type"] == "done" for event in events)
    assert sessions.opened == sessions.closed == 1


def test_sdk_does_not_retry_and_preserves_sanitized_upstream_failure(monkeypatch):
    sessions = Sessions()
    calls = []
    monkeypatch.setattr(endpoint, "async_session_maker", sessions)

    async def gateway(*args):
        calls.append(1)
        request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        response = httpx.Response(429, request=request, json={"error": {"message": "limit reached", "api_key": "secret"}})
        raise httpx.HTTPStatusError("error", request=request, response=response)
    monkeypatch.setattr(endpoint, "chat_completions", gateway)

    async def run():
        return [event async for event in endpoint._safe_chat_stream(conversation(), http_request(), SimpleNamespace(id=1))]
    result = "".join(asyncio.run(run()))
    assert "limit reached" in result
    assert "secret" not in result
    assert len(calls) == 1
    assert sessions.opened == sessions.closed == 1


@pytest.mark.parametrize("mode", ["close", "cancel"])
def test_stopping_stream_closes_iterator_callbacks_admission_and_db(monkeypatch, mode):
    sessions = Sessions()
    monkeypatch.setattr(endpoint, "async_session_maker", sessions)
    closed = []

    async def run():
        started = asyncio.Event()
        waiting = asyncio.Event()

        async def body():
            try:
                yield next(chat_sse("first"))
                started.set()
                await waiting.wait()
            finally:
                closed.append("iterator")

        async def close_upstream():
            await asyncio.sleep(0)
            closed.append("upstream")

        admission = SimpleNamespace(engine=SimpleNamespace(release_attempt=lambda value: closed.append("admission")))

        async def gateway(*args):
            return AdmissionStreamingResponse(body(), admission=admission, close_callbacks=(close_upstream,))
        monkeypatch.setattr(endpoint, "chat_completions", gateway)
        stream = endpoint._safe_chat_stream(conversation(), http_request(), SimpleNamespace(id=1))
        if mode == "close":
            assert '"first"' in await anext(stream)
            assert closed == []
            await stream.aclose()
        else:
            async def consume():
                async for event in stream:
                    pass
            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
    assert closed == ["iterator", "upstream", "admission"]
    assert sessions.opened == sessions.closed == 1


def test_unstarted_stream_is_closed_when_client_closes(monkeypatch):
    sessions = Sessions()
    monkeypatch.setattr(endpoint, "async_session_maker", sessions)
    closed = []

    async def gateway(*args):
        async def body():
            yield "not consumed"
        async def close_upstream():
            closed.append("upstream")
        admission = SimpleNamespace(engine=SimpleNamespace(release_attempt=lambda value: closed.append("admission")))
        return AdmissionStreamingResponse(body(), admission=admission, close_callbacks=(close_upstream,))
    monkeypatch.setattr(endpoint, "chat_completions", gateway)

    async def run():
        transport = endpoint._GatewayTransport(http_request(), SimpleNamespace(id=1), "chat-test")
        await transport.handle_async_request(httpx2.Request(
            "POST", "http://rotor.internal/v1/chat/completions", json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            },
        ))
        await transport.aclose()
        await transport.aclose()
    asyncio.run(run())
    assert closed == ["upstream", "admission"]
    assert sessions.opened == sessions.closed == 1


@pytest.mark.parametrize("api_format", ["chat", "responses"])
def test_connection_test_performs_real_sdk_generation_with_selected_settings(monkeypatch, api_format):
    observed = []
    sessions = Sessions()
    monkeypatch.setattr(endpoint, "async_session_maker", sessions)

    async def select_token(model, actor):
        assert model == "model-a"
        return SimpleNamespace(id=1)

    async def gateway(data, request, *args):
        observed.append(data)
        assert request.headers["x-conversation-id"].startswith("connection-test-")
        async def body():
            for item in (chat_sse("OK") if api_format == "chat" else responses_sse("OK")):
                yield item
        return StreamingResponse(body())
    monkeypatch.setattr(endpoint, "_chat_principal", select_token)
    monkeypatch.setattr(endpoint, "chat_completions", gateway)
    monkeypatch.setattr(endpoint, "create_response", gateway)
    result = asyncio.run(endpoint.test_connection(
        endpoint.ChatSettings(model="model-a", api_format=api_format, system_prompt="test system", temperature=0.2),
        http_request(), SimpleNamespace(user_id=1),
    ))
    assert result["success"] is True
    assert result["message"] == "OK"
    assert result["latency_ms"] >= 0
    data = observed[0]
    assert data.temperature == 0.2
    if api_format == "chat":
        assert len(data.messages) == 2
        assert data.messages[0].content == "test system"
        assert data.max_tokens is None
    else:
        assert len(data.input) == 1
        assert data.instructions == "test system"
        assert data.max_output_tokens is None
    assert sessions.closed == 1


def test_connection_test_failure_is_http_error(monkeypatch):
    async def select_token(model, actor):
        return SimpleNamespace(id=1)
    async def gateway(*args):
        raise HTTPException(403, "permission denied")
    monkeypatch.setattr(endpoint, "_chat_principal", select_token)
    monkeypatch.setattr(endpoint, "async_session_maker", Sessions())
    monkeypatch.setattr(endpoint, "chat_completions", gateway)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint.test_connection(endpoint.ChatSettings(model="model-a"), http_request(), SimpleNamespace(user_id=1)))
    assert caught.value.status_code == 403
    assert "permission denied" in caught.value.detail


def test_model_listing_and_selection_respect_same_account_channel_scope(monkeypatch):
    from rotor.core import user_access
    from rotor.models.admin_auth import AdminUser
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(endpoint, "async_session_maker", sessions)
        async with sessions() as db:
            db.add_all([
                AdminUser(id=1, username="first-user", password_hash="test", enabled=True, must_change_password=False,
                          role="admin", quota=None, allowed_channels=[2, 4], default_token_id=1),
                Channel(id=1, name="first", type="openai", base_url="https://one.invalid", key="k1", models=["one"], enabled=True),
                Channel(id=2, name="second", type="openai", base_url="https://two.invalid", key="k2", models=["two"], model_mapping={"alias": "real"}, enabled=True),
                Channel(id=3, name="hidden", type="openai", base_url="https://three.invalid", key="k3", models=["hidden"], enabled=True),
                Channel(id=4, name="disabled", type="openai", base_url="https://four.invalid", key="k4", models=["disabled"], enabled=False),
                Token(id=1, key="first", name="first", owner_id=1, allowed_channels=[1], enabled=True),
                Token(id=2, key="second", name="second", owner_id=1, allowed_channels=[2, 4], enabled=True),
                Token(id=3, key="expired", name="expired", allowed_channels=[3], expire_time=datetime.now(timezone.utc) - timedelta(days=1)),
                Token(id=4, key="quota", name="quota", allowed_channels=[3], quota=3, used_quota=3),
                Token(id=5, key="disabled", name="disabled", enabled=False),
            ])
            await db.commit()
        actor = SimpleNamespace(user_id=1)
        models = await endpoint.chat_models(actor)
        assert models == {"models": ["alias", "two"], "account_available": True}
        principal = await endpoint._chat_principal("alias", actor)
        assert isinstance(principal, user_access.AccountPrincipal) and principal.id is None and principal.owner_id == 1
        async with sessions() as db:
            assert await user_access.accessible_models(principal, db) == ["alias", "two"]
        with pytest.raises(HTTPException) as caught:
            await endpoint._chat_principal("one", actor)
        assert caught.value.status_code == 403
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"messages": [{"role": "assistant", "content": "answer"}]},
    {"messages": [{"role": "user", "content": ""}]},
    {"temperature": float("nan")}, {"temperature": -0.1}, {"temperature": 2.1},
    {"max_tokens": 0}, {"max_tokens": -10}, {"api_format": "unknown"},
])
def test_invalid_requests_are_rejected(changes):
    with pytest.raises(ValidationError):
        conversation(**changes)


@pytest.mark.parametrize("changes", [
    {"size": 0}, {"data_url": "https://image.invalid/image.png"},
    {"data_url": "data:image/png;base64,%%%%"}, {"mime_type": "image/svg+xml"},
    {"content": "text and binary"},
])
def test_invalid_image_payloads_are_rejected(changes):
    with pytest.raises(ValidationError):
        endpoint.ChatAttachment(**{**attachment(), **changes})


def test_image_type_and_real_size_are_checked():
    with pytest.raises(ValidationError, match="内容与声明"):
        endpoint.ChatAttachment(**attachment(b"not an image"))
    with pytest.raises(ValidationError):
        endpoint.ChatAttachment(**attachment(b"\x89PNG\r\n\x1a\n" + b"x" * 5_000_000))
    text = {"kind": "file", "name": "text.txt", "mime_type": "text/plain", "size": 0, "content": "你" * 200_000}
    with pytest.raises(ValidationError, match="500 KB"):
        endpoint.ChatAttachment(**text)
    large = attachment(b"\x89PNG\r\n\x1a\n" + b"x" * 3_500_000)
    with pytest.raises(ValidationError, match="10 MB"):
        conversation(messages=[{"role": "user", "content": "hi", "attachments": [large] * 3}])
    with pytest.raises(ValidationError, match="20 MB"):
        conversation(messages=[{"role": "user", "content": "hi", "attachments": [large] * 2}] * 3)


def docx_bytes(text="paragraph text"):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return result.getvalue()


def test_docx_text_is_extracted_and_embedded_in_both_api_formats():
    document = attachment(docx_bytes(), kind="file", mime=endpoint._DOCX_MIME, name="report.docx")
    async def run():
        for api_format in ("chat", "responses"):
            payload = await endpoint._sdk_payload(conversation(
                api_format=api_format, messages=[{"role": "user", "content": "", "attachments": [document]}],
            ))
            messages = payload.get("messages", payload.get("input"))
            assert "paragraph text" in messages[0]["content"][0]["text"]
    asyncio.run(run())


def test_docx_bombs_and_xml_entities_are_rejected():
    bomb = attachment(docx_bytes("x" * 5_000_000), kind="file", mime=endpoint._DOCX_MIME, name="huge.docx")
    with pytest.raises(ValueError, match="解压内容过大"):
        endpoint._document_text(endpoint.ChatAttachment(**bomb))
    malformed = io.BytesIO()
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE data [<!ENTITY x "expanded">]><data>&x;</data>')
    with pytest.raises(ValueError, match="XML 定义"):
        endpoint._document_text(endpoint.ChatAttachment(**attachment(
            malformed.getvalue(), kind="file", mime=endpoint._DOCX_MIME, name="invalid.docx"
        )))


def test_pdf_text_extraction_and_scanned_pdf_rejection():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 10 Td (Document facts) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    pdf = endpoint.ChatAttachment(**attachment(output.getvalue(), kind="file", mime="application/pdf", name="test.pdf"))
    assert "Document facts" in endpoint._document_text(pdf)
    blank = PdfWriter()
    blank.add_blank_page(200, 200)
    output = io.BytesIO()
    blank.write(output)
    pdf = endpoint.ChatAttachment(**attachment(output.getvalue(), kind="file", mime="application/pdf", name="scan.pdf"))
    with pytest.raises(ValueError, match="扫描文档"):
        endpoint._document_text(pdf)


def test_admin_chat_routes_require_session_authentication(monkeypatch):
    from rotor.main import app
    paths = app.openapi()["paths"]
    assert all(f"/api/admin/chat/{path}" in paths for path in ("models", "completions", "test"))
    assert not any(path.startswith("/api/admin/mindagent") for path in paths)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            for method, path in (("GET", "models"), ("POST", "completions"), ("POST", "test")):
                response = await client.request(method, f"/api/admin/chat/{path}", json={})
                assert response.status_code == 401
    asyncio.run(run())


def test_one_cleanup_failure_does_not_skip_remaining_resources(monkeypatch):
    sessions = Sessions()
    monkeypatch.setattr(endpoint, "async_session_maker", sessions)
    closed = []

    async def gateway(*args):
        async def body():
            yield next(chat_sse("text"))
        async def first():
            closed.append("first")
            raise RuntimeError("cleanup failed")
        async def second():
            closed.append("second")
        admission = SimpleNamespace(engine=SimpleNamespace(release_attempt=lambda value: closed.append("admission")))
        return AdmissionStreamingResponse(body(), admission=admission, close_callbacks=(first, second))
    monkeypatch.setattr(endpoint, "chat_completions", gateway)

    async def run():
        async with aclosing(endpoint._safe_chat_stream(conversation(), http_request(), SimpleNamespace(id=1))) as events:
            async for event in events:
                pass
    asyncio.run(run())
    assert closed == ["first", "second", "admission"]
    assert sessions.closed == 1


def test_upstream_unauthorized_does_not_log_out_admin(monkeypatch):
    async def select_token(model, actor):
        return SimpleNamespace(id=1)
    async def gateway(*args):
        raise HTTPException(401, {"error": {"message": "Upstream API key expired", "token": "sensitive"}})
    monkeypatch.setattr(endpoint, "_chat_principal", select_token)
    monkeypatch.setattr(endpoint, "async_session_maker", Sessions())
    monkeypatch.setattr(endpoint, "chat_completions", gateway)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint.test_connection(endpoint.ChatSettings(model="model-a"), http_request(), SimpleNamespace(user_id=1)))
    assert caught.value.status_code == 502
    assert "401" in caught.value.detail
    assert "sensitive" not in caught.value.detail


@pytest.mark.parametrize("name", ["source.py", "source.js", "source.ts", "source.go", "events.jsonl", "README.markdown"])
def test_text_code_files_with_browser_unknown_mime_are_supported(name):
    item = endpoint.ChatAttachment(kind="file", name=name, mime_type="application/octet-stream", size=3, content="abc")
    assert endpoint._document_text(item) == "abc"
