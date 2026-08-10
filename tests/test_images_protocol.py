import asyncio
from types import SimpleNamespace

import httpx

import rotor.api.v1.images as images_endpoint
from rotor.api.v1.images import (
    ImageGenerationRequest,
    _parse_sse_data,
    _record_failure,
    image_generation_headers,
    image_generation_url,
)
from rotor.gateway.attempts import AttemptContext
from rotor.main import app


def _channel(**overrides):
    values = {
        "id": 1,
        "name": "images",
        "type": "openai",
        "protocol": "openai_responses",
        "base_url": "https://example.com/v1",
        "key": "provider-secret",
        "extra": {
            "request_path": "/responses",
            "auth_type": "bearer",
        },
        "model_mapping": {"image-model": "provider-image-model"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_image_generation_route_is_registered() -> None:
    operation = app.openapi()["paths"]["/v1/images/generations"]

    assert "post" in operation


def test_image_generation_uses_dedicated_upstream_path() -> None:
    channel = _channel()

    assert image_generation_url(channel) == "https://example.com/v1/images/generations"


def test_image_generation_supports_custom_upstream_path() -> None:
    channel = _channel(extra={
        "images_path": "/custom/images/generations",
        "auth_type": "bearer",
    })

    assert image_generation_url(channel) == (
        "https://example.com/v1/custom/images/generations"
    )


def test_image_generation_preserves_extension_fields_and_maps_model() -> None:
    request = ImageGenerationRequest.model_validate({
        "model": "image-model",
        "prompt": "A small red house",
        "size": "1536x1024",
        "quality": "high",
        "output_format": "webp",
        "partial_images": 2,
        "stream": True,
    })

    payload = request.provider_payload("provider-image-model")

    assert payload == {
        "model": "provider-image-model",
        "prompt": "A small red house",
        "size": "1536x1024",
        "quality": "high",
        "output_format": "webp",
        "partial_images": 2,
        "stream": True,
    }


def test_image_generation_builds_provider_auth_and_stream_headers() -> None:
    headers = image_generation_headers(_channel(), stream=True)

    assert headers["Authorization"] == "Bearer provider-secret"
    assert headers["accept"] == "text/event-stream"


def test_image_generation_sse_parser_reads_usage() -> None:
    event = _parse_sse_data(
        "event: image_generation.completed\n"
        'data: {"type":"image_generation.completed","usage":'
        '{"input_tokens":2,"output_tokens":3,"total_tokens":5}}\n\n'
    )

    assert event["type"] == "image_generation.completed"
    assert event["usage"]["total_tokens"] == 5


def test_image_failure_records_normalized_attempt() -> None:
    class RecordingAccounting:
        def __init__(self) -> None:
            self.attempts = []

        async def record(self, **kwargs) -> bool:
            self.attempts.append(kwargs)
            kwargs["context"].recorded = True
            return True

        async def record_failure(self, db, **kwargs) -> None:
            return None

    class FakeDatabase:
        async def commit(self) -> None:
            return None

    request = httpx.Request("POST", "https://example.com/v1/images/generations")
    response = httpx.Response(503, request=request, json={"error": "unavailable"})
    error = httpx.HTTPStatusError(
        "unavailable",
        request=request,
        response=response,
    )
    original = images_endpoint.accounting_service
    original_attempt_recorder = images_endpoint.attempt_recorder
    accounting = RecordingAccounting()
    images_endpoint.accounting_service = accounting
    images_endpoint.attempt_recorder = accounting
    try:
        asyncio.run(
            _record_failure(
                FakeDatabase(),
                token=SimpleNamespace(id=1),
                channel=_channel(),
                model="image-model",
                error=error,
                request_id="req-1",
                conversation_id="conv-1",
                start_time=0.0,
                client_ip="127.0.0.1",
                attempt_context=AttemptContext.start(0),
            )
        )
    finally:
        images_endpoint.accounting_service = original
        images_endpoint.attempt_recorder = original_attempt_recorder

    assert accounting.attempts[0]["error"].code == "upstream_unavailable"
    assert accounting.attempts[0]["error"].fallback_allowed is True
