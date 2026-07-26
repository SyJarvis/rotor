from types import SimpleNamespace

from rotor.api.v1.images import (
    ImageGenerationRequest,
    _parse_sse_data,
    image_generation_headers,
    image_generation_url,
)
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
