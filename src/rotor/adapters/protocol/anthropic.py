from rotor.adapters.base import AnthropicCompatibleAdapter
from rotor.adapters.protocol.converter import ProtocolConverter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator, Any
from copy import deepcopy
from rotor.schemas.request import ChatCompletionRequest
from rotor.core.exceptions import UpstreamOverloaded, UpstreamProtocolError
from rotor.adapters.protocol.anthropic_integrity import (
    NativeMessageStream, iter_anthropic_sse, validate_native_message,
)
import json


class AnthropicAdapter(AnthropicCompatibleAdapter):
    """Adapter for Anthropic Claude API."""

    native_anthropic = True

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        headers = super().setup_request_headers(request)
        headers.update(request.anthropic_headers or {})
        return headers

    async def count_tokens(
        self,
        body: dict[str, Any],
        forwarded_headers: dict[str, str] | None = None,
    ) -> Response:
        payload = deepcopy(body)
        payload["model"] = self.map_model_name(str(payload["model"]))
        headers = self.build_request_headers()
        headers.update(forwarded_headers or {})
        url = f"{self.build_request_url('/messages').rstrip('/')}/count_tokens"
        response = await self.http_client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response

    async def convert_request(self, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert OpenAI request to Anthropic format."""
        if request.anthropic_payload is not None:
            # Keep cache_control, structured system blocks, tool-result error
            # state, and future Anthropic-native fields intact.
            anthropic_request = deepcopy(request.anthropic_payload)
        else:
            anthropic_request = ProtocolConverter.openai_to_anthropic(request)
        # Map model name if needed
        anthropic_request["model"] = self.map_model_name(request.model)
        return anthropic_request

    async def convert_response(self, response: Response, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert Anthropic response to OpenAI format."""
        try:
            data = response.json()
            validate_native_message(data, secret=self.channel.key)
        except (json.JSONDecodeError, UnicodeDecodeError):
            exc = UpstreamProtocolError("Upstream returned invalid Anthropic JSON")
            exc.upstream_status = response.status_code
            raise exc from None
        except (UpstreamOverloaded, UpstreamProtocolError) as exc:
            exc.upstream_status = response.status_code
            raise

        if request.anthropic_payload is not None:
            return data

        # Parse Anthropic response
        from rotor.schemas.request import AnthropicMessageResponse
        anthropic_response = AnthropicMessageResponse(**data)

        return ProtocolConverter.anthropic_to_openai(
            anthropic_response,
            request.model
        )

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """Convert Anthropic streaming response to OpenAI format."""
        import uuid
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        tracker = NativeMessageStream(secret=self.channel.key)
        try:
            async for event in iter_anthropic_sse(response, secret=self.channel.key):
                tracker.feed(event)
                if request.anthropic_payload is not None:
                    yield event
                    continue
                openai_chunk = ProtocolConverter.anthropic_stream_to_openai(event, request.model, chunk_id)
                if openai_chunk:
                    yield openai_chunk
            tracker.finish()
        except (UpstreamOverloaded, UpstreamProtocolError) as exc:
            exc.upstream_status = response.status_code
            raise
