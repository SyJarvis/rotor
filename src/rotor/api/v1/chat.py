import asyncio
import time
import uuid
import logging
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.database import async_session_maker, get_db
from rotor.core.client_session import resolve_client_session
from rotor.core.deps import get_current_token, get_available_channels
from rotor.application_settings import application_settings
from rotor.schemas.request import (
    ChatMessage,
    ChatCompletionRequest,
    Role,
)
from rotor.schemas.error import ErrorPhase
from rotor.adapters.factory import AdapterFactory
from rotor.adapters.protocol.openai_integrity import ChatStreamIntegrity, validate_chat_response
from rotor.adapters.protocol.responses import validate_responses_response
from rotor.gateway.accounting import AccountingService, StreamingUsageAccumulator
from rotor.gateway.provider_facts import (
    extract_capacity_snapshot,
    extract_capacity_snapshot_from_error,
)
from rotor.gateway.attempts import AttemptContext, attempt_recorder
from rotor.gateway.capabilities import chat_required_capabilities
from rotor.conversations.store import ConversationHandle, ConversationStore
from rotor.gateway.routing import routing_engine, session_lease_success_reason
from rotor.services.session_leases import get_session_lease_preference
from rotor.gateway.streaming import AdmissionStreamingResponse
from rotor.gateway.fallback import (
    retry_after_seconds,
    set_routing_headers,
    should_fallback,
)
from rotor.models.channel import Channel
from rotor.core.exceptions import (
    ChannelException,
    ChannelsTemporarilyUnavailable,
    UpstreamOverloaded,
    UpstreamProtocolError,
    classify_error_status,
    format_error_message,
    normalize_upstream_error,
    upstream_error_payload,
)
from httpx import AsyncClient, HTTPStatusError, RequestError

logger = logging.getLogger(__name__)

router = APIRouter()
accounting_service = AccountingService()
conversation_store = ConversationStore()


class EmptyUpstreamResponse(UpstreamProtocolError):
    """The provider closed a stream without producing a completion."""


def _observed_error_usage(error):
    usage = getattr(error, "provider_usage", None)
    return accounting_service.extract_usage({"usage": usage}) if usage is not None else None


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    api_response: Response,
    db: AsyncSession = Depends(get_db),
    token = Depends(get_current_token),
):
    """
    OpenAI-compatible chat completions endpoint.

    Supports:
    - Multiple providers with automatic failover
    - Streaming and non-streaming responses
    - Token usage tracking
    - Request logging
    """
    # Get available channels for the model
    channels = await get_available_channels(request.model, token, db)

    request_id = http_request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
    client_session = resolve_client_session(
        http_request.headers,
        legacy_user_id=request.user,
    )
    conversation_id = client_session.session_id or f"conv_{uuid.uuid4().hex[:24]}"
    client_ip = http_request.client.host if http_request.client else "unknown"
    request_origin = getattr(http_request.state, "request_origin", "client")
    agent_run_id = getattr(http_request.state, "agent_run_id", None)

    # Create routing decision
    routing_settings = application_settings.get().routing
    lease_session_id = (
        client_session.session_id
        if routing_settings.affinity_enabled
        and routing_settings.session_lease_enabled
        else None
    )
    required_capabilities = chat_required_capabilities(request)
    lease_preference = await get_session_lease_preference(
        db,
        token_id=token.id,
        session_id=lease_session_id,
        logical_model=request.model,
        request_protocol="openai_chat",
        channels=channels,
        active_native_channel_ids=routing_engine.available_native_channel_ids(
            channels, request.model, "openai_chat", required_capabilities
        ),
        reassess_seconds=(
            routing_settings.session_lease_reassess_seconds
            if routing_settings.affinity_enabled and routing_engine.protocol_affinity_enabled else 0
        ),
    )
    affinity_key = (
        client_session.affinity_key(token.id)
        if routing_settings.affinity_enabled
        else None
    )
    affinity_used = affinity_key is not None
    routing_decision = routing_engine.route(
        channels,
        model=request.model,
        token=token,
        request_protocol="openai_chat",
        required_capabilities=required_capabilities,
        affinity_key=affinity_key,
        preferred_channel_id=lease_preference.channel_id,
        lease_reassessment_due=lease_preference.reassessment_due,
    )
    candidates = routing_decision.candidates
    if not candidates:
        if routing_decision.temporarily_unavailable:
            raise ChannelsTemporarilyUnavailable(request.model, routing_decision.retry_after_seconds)
        raise ChannelException(
            f"No compatible channel available for Chat model '{request.model}'. "
            f"Required capabilities: {sorted(required_capabilities)}",
            status_code=503,
        )
    # Defer the routing-decision write: it shares the request transaction
    # with usage accounting below, so the hot path performs one commit
    # instead of one before the upstream call plus one after it.
    # NOTE: intentionally no flush here — the INSERT is batched with the
    # final accounting commit.
    accounting_service.record_routing_decision(
        db,
        request_id=request_id,
        token=token,
        model=request.model,
        request_protocol="openai_chat",
        decision=routing_decision,
        required_capabilities=required_capabilities,
        affinity_used=affinity_used,
        features={
            "stream": bool(request.stream),
            "message_count": len(request.messages),
            "has_tools": bool(request.tools),
            **client_session.routing_features(),
        },
    )

    # Record start time
    start_time = time.time()

    # Try to get a response from available channels
    http_client = AsyncClient(timeout=120.0)
    streaming_response_returned = False
    last_error = None

    conversation_handle = await conversation_store.start(
        db,
        conversation_id=conversation_id,
        request_id=request_id,
        token=token,
        request=request,
        protocol="openai_chat",
    )
    try:
        for attempt, channel in enumerate(candidates):
            admission = routing_engine.admit_attempt(request.model, channel)
            if admission is None:
                continue
            attempt_context = AttemptContext.start(
                attempt,
                request_origin=request_origin,
                agent_run_id=agent_run_id,
            )
            attempt_context.admission = admission
            try:
                # Create adapter
                adapter = AdapterFactory.create_adapter(channel, http_client)
                await conversation_store.append_routing(conversation_handle, channel)

                # Make request
                if request.stream:
                    response = await adapter.make_request(request)
                    result = await _handle_streaming_request(
                        request, adapter, channel, token, db, start_time,
                        http_request, http_client, request_id, conversation_handle,
                        response=response,
                        attempt_context=attempt_context,
                        lease_session_id=lease_session_id,
                        lease_migration_reason=session_lease_success_reason(
                            routing_decision, attempt, channel=channel, request_protocol="openai_chat"
                        ),
                    )
                    set_routing_headers(result, channel, request.model, attempt > 0)
                    # Ownership of http_client transfers to the streaming
                    # generator, which closes it in its own finally block.
                    streaming_response_returned = True
                    return result
                else:
                    set_routing_headers(
                        api_response, channel, request.model, attempt > 0
                    )
                    return await _handle_non_streaming_request(
                        request, adapter, channel, token, db, start_time, http_request,
                        request_id, conversation_handle,
                        attempt_context=attempt_context,
                        lease_session_id=lease_session_id,
                        lease_migration_reason=session_lease_success_reason(
                            routing_decision, attempt, channel=channel, request_protocol="openai_chat"
                        ),
                    )

            except (HTTPStatusError, RequestError) as e:
                last_error = e
                if should_fallback(e):
                    routing_engine.mark_unavailable(request.model, channel, retry_after_seconds(e))
                logger.warning(f"Channel {channel.name} failed: {format_error_message(e)}")
                latency_ms = int((time.time() - start_time) * 1000)
                attempt_latency_ms = attempt_context.elapsed_ms()
                error_fact = normalize_upstream_error(e)
                await attempt_recorder.record(
                    context=attempt_context,
                    request_id=request_id,
                    channel=channel,
                    requested_model=request.model,
                    provider_model=(channel.model_mapping or {}).get(
                        request.model, request.model
                    ),
                    request_protocol="openai_chat",
                    outcome="failed",
                    error=error_fact,
                    db=db,
                )
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="openai_chat",
                    token=token,
                    channel=channel,
                    model=request.model,
                    error_code=type(e).__name__,
                    error_message=format_error_message(e),
                    latency_ms=latency_ms,
                    attempt_latency_ms=attempt_latency_ms,
                    admission=admission,
                    client_ip=client_ip,
                    provider_response=upstream_error_payload(e),
                    capacity_snapshot=extract_capacity_snapshot_from_error(e),
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle,
                        type(e).__name__,
                        format_error_message(e),
                    )
                await db.commit()
                if should_fallback(e):
                    continue
                if conversation_handle:
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                raise

            except Exception as e:
                last_error = e
                if should_fallback(e):
                    routing_engine.mark_unavailable(request.model, channel, retry_after_seconds(e))
                logger.error(f"Unexpected error with channel {channel.name}: {format_error_message(e)}")
                latency_ms = int((time.time() - start_time) * 1000)
                attempt_latency_ms = attempt_context.elapsed_ms()
                if isinstance(e, (UpstreamProtocolError, UpstreamOverloaded)) and not attempt_context.recorded:
                    await attempt_recorder.record(
                        context=attempt_context,
                        request_id=request_id,
                        channel=channel,
                        requested_model=request.model,
                        provider_model=(channel.model_mapping or {}).get(request.model, request.model),
                        request_protocol="openai_chat",
                        outcome="failed",
                        error=normalize_upstream_error(e),
                        db=db,
                    )
                await accounting_service.record_failure(
                    db,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    request_protocol="openai_chat",
                    token=token,
                    channel=channel,
                    model=request.model,
                    error_code=type(e).__name__,
                    error_message=format_error_message(e),
                    latency_ms=latency_ms,
                    attempt_latency_ms=attempt_latency_ms,
                    admission=admission,
                    client_ip=client_ip,
                    capacity_snapshot=extract_capacity_snapshot_from_error(e),
                    usage=_observed_error_usage(e),
                )
                if conversation_handle:
                    await conversation_store.append_error(
                        conversation_handle,
                        type(e).__name__,
                        format_error_message(e),
                    )
                await db.commit()
                if should_fallback(e):
                    continue
                if conversation_handle:
                    await conversation_store.finish(conversation_handle, "failed", latency_ms)
                if isinstance(e, (UpstreamProtocolError, UpstreamOverloaded)):
                    raise ChannelException(
                        "Upstream returned an invalid completion",
                        status_code=classify_error_status(e),
                        original_error=format_error_message(e),
                    ) from e
                raise
            finally:
                if not streaming_response_returned:
                    admission.engine.release_attempt(admission)
    finally:
        # Close the client unless a streaming response took ownership of it.
        # This covers non-streaming requests and streaming requests whose
        # make_request() failed on every candidate (the generator that would
        # normally close the client never started).
        if not streaming_response_returned:
            await http_client.aclose()

    # All channels failed
    await conversation_store.finish(
        conversation_handle, "failed", int((time.time() - start_time) * 1000)
    )
    if last_error is None:
        raise ChannelsTemporarilyUnavailable(request.model, routing_engine.retry_after_seconds(request.model, candidates))
    raise ChannelException(
        f"All channels failed for model '{request.model}'",
        status_code=classify_error_status(last_error),
        original_error=format_error_message(last_error),
    )


async def _handle_non_streaming_request(
    request: ChatCompletionRequest,
    adapter,
    channel: Channel,
    token,
    db: AsyncSession,
    start_time: float,
    http_request: Request,
    request_id: str,
    conversation_handle: ConversationHandle,
    request_protocol: str = "openai_chat",
    attempt_context: AttemptContext | None = None,
    lease_session_id: str | None = None,
    lease_migration_reason: str = "request_success",
    response_transform=None,
) -> dict:
    """Handle non-streaming chat completion request."""
    admission = attempt_context.admission if attempt_context is not None else None
    # Make the request
    response = await adapter.make_request(request)

    # Convert response
    try:
        response_data = await adapter.convert_response(response, request)
        if isinstance(response_data, dict) and response_data.get("object") == "response":
            validate_responses_response(response_data, allow_background=request_protocol == "openai_responses")
        else:
            validate_chat_response(
                response_data, upstream_status=response.status_code,
                secret=getattr(channel, "key", None), expected_choices=request.n,
            )
        client_response = response_transform(response_data) if response_transform else response_data
        if response_transform:
            validate_responses_response(client_response, allow_background=True)
    except (UpstreamProtocolError, UpstreamOverloaded) as exc:
        exc.upstream_status = response.status_code
        raise
    if admission is not None:
        admission.provider_succeeded = True

    latency_ms = int((time.time() - start_time) * 1000)
    attempt_latency_ms = (
        attempt_context.elapsed_ms() if attempt_context is not None else None
    )
    usage = accounting_service.extract_usage(response_data)
    provider_model = adapter.map_model_name(request.model)
    client_ip = http_request.client.host if http_request.client else "unknown"

    deferred_background_usage = (
        request_protocol == "openai_responses"
        and response_data.get("object") == "response"
        and response_data.get("status") in {"queued", "in_progress"}
    )
    if attempt_context is not None:
        # Join the request transaction: one commit covers the routing
        # decision, this attempt, and usage accounting below.
        await attempt_recorder.record(
            context=attempt_context,
            request_id=request_id,
            channel=channel,
            requested_model=request.model,
            provider_model=provider_model,
            request_protocol=request_protocol,
            outcome="success",
            db=db,
        )
    if not deferred_background_usage:
        await accounting_service.record_success(
            db,
            request_id=request_id,
            conversation_id=conversation_handle.conversation_id,
            request_protocol=request_protocol,
            token=token,
            channel=channel,
            model=request.model,
            provider_model=provider_model,
            usage=usage,
            latency_ms=latency_ms,
            attempt_latency_ms=attempt_latency_ms,
            admission=admission,
            client_ip=client_ip,
            capacity_snapshot=extract_capacity_snapshot(
                getattr(response, "headers", {})
            ),
            tariff_at=(
                attempt_context.started_at
                if attempt_context is not None
                else start_time
            ),
            lease_session_id=lease_session_id,
            lease_migration_reason=lease_migration_reason,
        )
    else:
        # A nonterminal native Responses object establishes ownership, but
        # any intermediate usage must wait for a terminal polling result.
        await accounting_service.record_lease_success(
            db,
            request_id=request_id,
            token=token,
            channel=channel,
            model=request.model,
            lease_session_id=lease_session_id,
            lease_migration_reason=lease_migration_reason,
        )
        if attempt_latency_ms is not None:
            (admission.engine if admission is not None else routing_engine).observe_result(
                request.model,
                channel,
                success=True,
                latency_ms=attempt_latency_ms,
                admission=admission,
            )
    await conversation_store.append_response(conversation_handle, response_data)
    await conversation_store.append_usage(conversation_handle, response_data.get("usage") or {})
    await conversation_store.finish(conversation_handle, "success", latency_ms)

    await db.commit()

    return client_response


async def _handle_streaming_request(
    request: ChatCompletionRequest,
    adapter,
    channel: Channel,
    token,
    db: AsyncSession,
    start_time: float,
    http_request: Request,
    http_client: AsyncClient,
    request_id: str,
    conversation_handle: ConversationHandle,
    request_protocol: str = "openai_chat",
    stream_transform=None,
    response=None,
    native_responses_stream: bool = False,
    native_response_callback=None,
    attempt_context: AttemptContext | None = None,
    lease_session_id: str | None = None,
    lease_migration_reason: str = "request_success",
) -> StreamingResponse:
    """Handle streaming chat completion request.

    The routing-decision row was staged on ``db`` before the upstream call.
    Commit it now: streaming accounting runs on a fresh session after the
    body completes, and the request session is about to be closed (which
    would otherwise roll the staged row back). The decision is immutable
    audit data, so committing it early does not affect the single-commit
    accounting of attempt + usage rows below. Test doubles may not
    implement ``commit``; skip it then (there is nothing real to persist).
    """

    # FastAPI keeps yield-based dependencies alive until a StreamingResponse
    # finishes.  Release the request session now so a slow client/provider does
    # not occupy a pool connection for the lifetime of the stream.  Accounting
    # below uses a short-lived session once streaming has completed.
    admission = attempt_context.admission if attempt_context is not None else None
    if hasattr(db, "commit"):
        await db.commit()
    await db.close()

    async def stream_generator():
        """Generator for streaming responses."""
        try:
            # Track token usage for streaming
            stream_usage = StreamingUsageAccumulator()
            collected_text: list[str] = []
            collected_tool_calls: dict[int, dict] = {}
            saw_meaningful_output = False
            last_finish_reason: str | None = None
            completed_native_response: dict | None = None
            latest_native_response: dict | None = None
            last_native_sequence = -1
            chat_integrity = ChatStreamIntegrity(
                upstream_status=getattr(response, "status_code", None),
                secret=getattr(channel, "key", None), expected_choices=request.n,
            )
            deferred_native_terminal: dict | None = None
            accounting_recorded = False
            accounting_response = response

            async def close_upstream_response(stream_response) -> None:
                """Close one upstream response without masking stream errors."""
                close = getattr(stream_response, "aclose", None)
                if not callable(close):
                    return
                try:
                    await close()
                except Exception:
                    logger.warning(
                        "Closing upstream stream failed for request_id=%s",
                        request_id,
                        exc_info=True,
                    )

            def has_nonempty_payload(value) -> bool:
                """Return whether a protocol payload contains any value."""
                return bool(value)

            def has_meaningful_output(chunk: dict) -> bool:
                """Return whether a chat chunk contains output worth replaying.

                Providers use a few extensions for valid non-text output.  Do
                not treat role/finish metadata alone as output, but recognize
                refusal, audio, legacy function calls, and reasoning deltas so
                those streams are not replayed or rejected as empty.
                """
                if not isinstance(chunk, dict):
                    return False
                # A non-empty usage snapshot is evidence that the provider
                # completed work even when the generated text is empty.
                if has_nonempty_payload(chunk.get("usage")):
                    return True
                output_fields = (
                    "content",
                    "tool_calls",
                    "function_call",
                    "refusal",
                    "audio",
                    "reasoning_content",
                    "reasoning",
                    "reasoning_details",
                    "thinking",
                    "thinking_content",
                    "output_text",
                    "output",
                )

                def payload_has_output(payload) -> bool:
                    if not isinstance(payload, dict):
                        return False
                    return any(
                        has_nonempty_payload(payload.get(field))
                        for field in output_fields
                    )

                choices = chunk.get("choices") or []
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    if payload_has_output(choice):
                        return True
                    if payload_has_output(choice.get("delta")):
                        return True
                    if payload_has_output(choice.get("message")):
                        return True
                return payload_has_output(chunk)

            def is_nonempty_stream_event(chunk: dict) -> bool:
                """Stop retrying on output or an explicit provider error."""
                return (
                    isinstance(chunk, dict)
                    and chunk.get("error") is not None
                ) or has_meaningful_output(chunk)

            async def stream_with_empty_retry(initial_response):
                """Retry one empty non-native stream before failing the request.

                Prelude/finish-only chunks are held until the stream proves it
                contains output. This makes the retry invisible to clients and
                prevents replaying any text or tool-call delta.
                """
                nonlocal accounting_response
                allow_retry = not native_responses_stream
                if not allow_retry:
                    try:
                        async for stream_chunk in adapter.stream_convert_response(
                            initial_response,
                            request,
                        ):
                            yield stream_chunk
                    finally:
                        await close_upstream_response(initial_response)
                    return

                current_response = initial_response
                for stream_index in range(2):
                    pending_chunks: list[dict] = []
                    meaningful = False
                    try:
                        async for stream_chunk in adapter.stream_convert_response(
                            current_response,
                            request,
                        ):
                            if meaningful:
                                yield stream_chunk
                                continue
                            if is_nonempty_stream_event(stream_chunk):
                                meaningful = True
                                for pending_chunk in pending_chunks:
                                    yield pending_chunk
                                pending_chunks.clear()
                                yield stream_chunk
                            else:
                                pending_chunks.append(stream_chunk)
                    finally:
                        await close_upstream_response(current_response)

                    if meaningful:
                        return
                    if stream_index == 1:
                        raise EmptyUpstreamResponse(
                            "Upstream returned an empty stream"
                        )

                    make_request = getattr(adapter, "make_request", None)
                    if not callable(make_request):
                        raise EmptyUpstreamResponse(
                            "Upstream returned an empty stream"
                        )
                    logger.warning(
                        "Upstream returned an empty stream; retrying once "
                        "request_id=%s channel_id=%s",
                        request_id,
                        getattr(channel, "id", None),
                    )
                    current_response = await make_request(request)
                    accounting_response = current_response

            if stream_transform:
                for event in stream_transform.start():
                    yield f"data: {_json_dumps(event)}\n\n"

            async for chunk in stream_with_empty_retry(response):
                if not native_responses_stream:
                    chat_integrity.feed(chunk)
                if has_meaningful_output(chunk):
                    saw_meaningful_output = True
                native_event_type = chunk.get("type") if native_responses_stream else None
                if native_event_type in {
                    "response.completed",
                    "response.failed",
                    "response.cancelled",
                    "response.incomplete",
                }:
                    # Codex disconnects as soon as it receives the terminal
                    # event. Defer it until accounting has released its DB
                    # session so cancellation cannot interrupt SQLite cleanup.
                    deferred_native_terminal = chunk
                elif native_responses_stream:
                    yield f"data: {_json_dumps(chunk)}\n\n"
                elif stream_transform:
                    for event in stream_transform.feed(chunk):
                        yield f"data: {_json_dumps(event)}\n\n"
                else:
                    yield f"data: {_json_dumps(chunk)}\n\n"

                if native_responses_stream:
                    event_type = native_event_type
                    sequence = chunk.get("sequence_number")
                    if isinstance(sequence, int):
                        last_native_sequence = max(last_native_sequence, sequence)
                    if event_type in {
                        "response.created",
                        "response.in_progress",
                        "response.completed",
                        "response.failed",
                        "response.cancelled",
                        "response.incomplete",
                    }:
                        native_response = chunk.get("response") or {}
                        latest_native_response = native_response
                        if native_response_callback and native_response.get("id"):
                            await native_response_callback(native_response, False)
                        if event_type in {"response.completed", "response.incomplete"}:
                            completed_native_response = native_response

                # Accumulate content/tool_calls/finish_reason for archiving.
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        collected_text.append(text)
                    for fallback_index, tc in enumerate(delta.get("tool_calls") or []):
                        tool_index = int(tc.get("index", fallback_index))
                        state = collected_tool_calls.setdefault(tool_index, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if tc.get("id"):
                            state["id"] = tc["id"]
                        if tc.get("type"):
                            state["type"] = tc["type"]
                        function = tc.get("function") or {}
                        if function.get("name"):
                            state["function"]["name"] = function["name"]
                        arguments = function.get("arguments")
                        if arguments:
                            if not isinstance(arguments, str):
                                arguments = _json_dumps(arguments)
                            state["function"]["arguments"] += arguments
                    fr = choices[0].get("finish_reason")
                    if fr:
                        last_finish_reason = fr

                # Try to extract usage info if available
                usage = chunk.get("usage") or {}
                if native_responses_stream and completed_native_response is not None:
                    usage = completed_native_response.get("usage") or {}
                if usage:
                    stream_usage.observe(accounting_service.extract_usage(
                        {"usage": usage}
                    ))

            # A provider that closes a chat stream without any content, tool
            # call, or usage has not produced a completion.  A finish marker
            # alone is not enough: the incident that motivated this guard
            # emitted an empty response with a stop marker.  Route this
            # through the existing failure path so it cannot be recorded as a
            # successful zero-token request.  Tool-call-only responses and
            # explicit provider usage snapshots remain valid.
            if (
                not saw_meaningful_output
                and not collected_text
                and not collected_tool_calls
                and not (
                    completed_native_response
                    and completed_native_response.get("output")
                )
                and not stream_usage.has_provider_usage
            ):
                raise EmptyUpstreamResponse("Upstream returned an empty stream")

            if native_responses_stream:
                if completed_native_response is None:
                    raise UpstreamProtocolError("Responses stream ended without a valid terminal response")
            else:
                chat_integrity.finish()

            prompt_tokens = stream_usage.prompt_tokens
            completion_tokens = stream_usage.completion_tokens
            cached_tokens = stream_usage.cached_tokens
            uncached_input_tokens = stream_usage.uncached_input_tokens
            cache_write_tokens = stream_usage.cache_write_tokens
            cache_write_5m_tokens = stream_usage.cache_write_5m_tokens
            cache_write_1h_tokens = stream_usage.cache_write_1h_tokens

            terminal_events: list[str] = []
            if native_responses_stream:
                if deferred_native_terminal is not None:
                    terminal_events.append(
                        f"data: {_json_dumps(deferred_native_terminal)}\n\n"
                    )
            elif stream_transform:
                for event in stream_transform.finish({
                    "input_tokens": prompt_tokens,
                    "input_tokens_details": {
                        "cached_tokens": cached_tokens,
                        "cache_write_tokens": cache_write_tokens,
                        "cache_write_5m_tokens": cache_write_5m_tokens,
                        "cache_write_1h_tokens": cache_write_1h_tokens,
                        "uncached_tokens": uncached_input_tokens,
                    },
                    "output_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                } if stream_usage.has_provider_usage else None):
                    terminal_events.append(f"data: {_json_dumps(event)}\n\n")
            else:
                terminal_events.append("data: [DONE]\n\n")

            # Update stats (estimated for streaming)
            if admission is not None:
                admission.provider_succeeded = True
            total_tokens = prompt_tokens + completion_tokens
            usage_data = stream_usage.to_usage_data(accounting_service)
            latency_ms = int((time.time() - start_time) * 1000)
            attempt_latency_ms = (
                attempt_context.elapsed_ms() if attempt_context is not None else None
            )
            client_ip = http_request.client.host if http_request.client else "unknown"
            # Accounting writes to SQLite; a lock collision here must never
            # mutate the response already sent to the client. Log and move on.
            try:
                # The attempt joins the accounting transaction: one commit
                # covers the attempt, usage ledger, and token counters.
                async with async_session_maker() as stream_db:
                    if attempt_context is not None:
                        await attempt_recorder.record(
                            context=attempt_context,
                            request_id=request_id,
                            channel=channel,
                            requested_model=request.model,
                            provider_model=adapter.map_model_name(request.model),
                            request_protocol=request_protocol,
                            outcome="success",
                            db=stream_db,
                        )
                    stream_token = await stream_db.get(type(token), token.id)
                    if stream_token is None:
                        raise RuntimeError(f"Token {token.id} no longer exists")
                    await accounting_service.record_success(
                        stream_db,
                        request_id=request_id,
                        conversation_id=conversation_handle.conversation_id,
                        request_protocol=request_protocol,
                        token=stream_token,
                        channel=channel,
                        model=request.model,
                        provider_model=adapter.map_model_name(request.model),
                        usage=usage_data,
                        latency_ms=latency_ms,
                        attempt_latency_ms=attempt_latency_ms,
                        admission=admission,
                        client_ip=client_ip,
                        capacity_snapshot=extract_capacity_snapshot(
                            getattr(accounting_response, "headers", {})
                        ),
                        tariff_at=(
                            attempt_context.started_at
                            if attempt_context is not None
                            else start_time
                        ),
                        lease_session_id=lease_session_id,
                        lease_migration_reason=lease_migration_reason,
                    )
                    await stream_db.commit()
                    accounting_recorded = True
            except Exception:
                logger.exception(
                    "Accounting record_success failed for request_id=%s; "
                    "stream response already complete, continuing",
                    request_id,
                )
            if native_responses_stream and completed_native_response is not None:
                if native_response_callback and accounting_recorded:
                    await native_response_callback(completed_native_response, True)
                await conversation_store.append_response(
                    conversation_handle,
                    completed_native_response,
                )
                await conversation_store.append_usage(
                    conversation_handle,
                    completed_native_response.get("usage") or {},
                )
            else:
                await conversation_store.append_response(conversation_handle, {
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "".join(collected_text) or None,
                            "tool_calls": (
                                [
                                    collected_tool_calls[index]
                                    for index in sorted(collected_tool_calls)
                                ]
                                or None
                            ),
                        },
                        "finish_reason": last_finish_reason or "stop",
                    }],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "prompt_tokens_details": {
                            "cached_tokens": cached_tokens,
                            "cache_write_tokens": cache_write_tokens,
                            "cache_write_5m_tokens": cache_write_5m_tokens,
                            "cache_write_1h_tokens": cache_write_1h_tokens,
                            "uncached_tokens": uncached_input_tokens,
                        },
                    },
                })
                await conversation_store.append_usage(conversation_handle, {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "prompt_tokens_details": {
                        "cached_tokens": cached_tokens,
                        "cache_write_tokens": cache_write_tokens,
                        "cache_write_5m_tokens": cache_write_5m_tokens,
                        "cache_write_1h_tokens": cache_write_1h_tokens,
                        "uncached_tokens": uncached_input_tokens,
                    },
                })
            await conversation_store.finish(conversation_handle, "success", latency_ms)
            for terminal_event in terminal_events:
                yield terminal_event

        except asyncio.CancelledError:
            latency_ms = int((time.time() - start_time) * 1000)
            try:
                if attempt_context is not None:
                    await attempt_recorder.record(
                        context=attempt_context,
                        request_id=request_id,
                        channel=channel,
                        requested_model=request.model,
                        provider_model=adapter.map_model_name(request.model),
                        request_protocol=request_protocol,
                        outcome="cancelled",
                    )
            except Exception:
                logger.exception(
                    "Recording cancelled attempt failed for request_id=%s",
                    request_id,
                )
            await conversation_store.finish(
                conversation_handle,
                "cancelled",
                latency_ms,
            )
            raise

        except Exception as e:
            if isinstance(e, (UpstreamProtocolError, UpstreamOverloaded)):
                e.upstream_status = getattr(accounting_response, "status_code", None)
            error_usage = _observed_error_usage(e)
            if error_usage is not None:
                stream_usage.observe(error_usage)
            logger.error(f"Streaming error: {format_error_message(e)}")
            if isinstance(e, EmptyUpstreamResponse) or should_fallback(e):
                routing_engine.mark_unavailable(request.model, channel, retry_after_seconds(e))
            latency_ms = int((time.time() - start_time) * 1000)
            attempt_latency_ms = (
                attempt_context.elapsed_ms() if attempt_context is not None else None
            )
            client_ip = http_request.client.host if http_request.client else "unknown"
            # Defensive try/except: accounting failure must not swallow the
            # error chunk we send to the client below. The failed attempt
            # joins the same accounting transaction (single commit).
            try:
                async with async_session_maker() as stream_db:
                    if attempt_context is not None and not attempt_context.recorded:
                        await attempt_recorder.record(
                            context=attempt_context,
                            request_id=request_id,
                            channel=channel,
                            requested_model=request.model,
                            provider_model=adapter.map_model_name(request.model),
                            request_protocol=request_protocol,
                            outcome="failed",
                            error=normalize_upstream_error(
                                e,
                                phase=ErrorPhase.PROVIDER_STREAM,
                            ),
                            db=stream_db,
                        )
                    await accounting_service.record_failure(
                        stream_db,
                        request_id=request_id,
                        conversation_id=conversation_handle.conversation_id,
                        request_protocol=request_protocol,
                        token=token,
                        channel=channel,
                        model=request.model,
                        error_code=type(e).__name__,
                        error_message=format_error_message(e),
                        latency_ms=latency_ms,
                        attempt_latency_ms=attempt_latency_ms,
                        admission=admission,
                        client_ip=client_ip,
                        capacity_snapshot=extract_capacity_snapshot_from_error(e),
                        usage=(stream_usage.to_usage_data(accounting_service) if stream_usage.has_provider_usage else None),
                    )
                    await stream_db.commit()
            except Exception:
                logger.exception(
                    "Accounting record_failure failed for request_id=%s; "
                    "sending error chunk to client anyway",
                    request_id,
                )
            await conversation_store.append_error(conversation_handle, type(e).__name__, format_error_message(e))
            await conversation_store.finish(conversation_handle, "failed", latency_ms)
            # Send an error matching the client-facing streaming protocol.
            error_message = format_error_message(e)
            if stream_transform and hasattr(stream_transform, "fail"):
                for event in stream_transform.fail(error_message, type(e).__name__):
                    yield f"data: {_json_dumps(event)}\n\n"
            elif request_protocol == "openai_responses":
                error_event = {
                    "type": "response.failed",
                    "sequence_number": last_native_sequence + 1,
                    "response": {
                        "id": (latest_native_response or {}).get("id") or f"resp_{uuid.uuid4().hex[:24]}",
                        "object": "response",
                        "created_at": int(time.time()),
                        "status": "failed",
                        "model": request.model,
                        "output": [],
                        "error": {
                            "code": "server_error",
                            "message": error_message,
                        },
                    },
                }
                yield f"data: {_json_dumps(error_event)}\n\n"
            else:
                error_chunk = {
                    "error": {
                        "message": error_message,
                        "type": "stream_error"
                    }
                }
                yield f"data: {_json_dumps(error_chunk)}\n\n"

        finally:
            if admission is not None:
                admission.engine.release_attempt(admission)
            if accounting_response is not None:
                await accounting_response.aclose()
            # Close the HTTP client after streaming completes
            await http_client.aclose()

    return AdmissionStreamingResponse(
        stream_generator(),
        admission=admission,
        close_callbacks=(http_client.aclose,) + ((response.aclose,) if response is not None else ()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


def _json_dumps(data: dict) -> str:
    """JSON dump with special handling."""
    import json
    return json.dumps(data, ensure_ascii=False)


def responses_input_to_messages(input_data) -> list[ChatMessage]:
    """Convert a minimal OpenAI Responses API input into chat messages."""
    if isinstance(input_data, str):
        return [ChatMessage(role=Role.USER, content=input_data)]

    messages: list[ChatMessage] = []
    if isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, str):
                messages.append(ChatMessage(role=Role.USER, content=item))
                continue
            if not isinstance(item, dict):
                continue

            role = item.get("role", "user")
            # Responses API allows role="developer" (OpenAI reasoning models,
            # equivalent to system). Normalize so downstream ChatMessage stays
            # within the supported enum and provider chat APIs accept it.
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            text_parts: list[str] = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") in ("input_text", "text", "output_text"):
                            text_parts.append(block.get("text", ""))
            messages.append(ChatMessage(role=Role(role), content="".join(text_parts)))

    return messages or [ChatMessage(role=Role.USER, content="")]
