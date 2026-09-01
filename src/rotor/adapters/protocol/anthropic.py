from rotor.adapters.base import AnthropicCompatibleAdapter
from rotor.adapters.protocol.converter import ProtocolConverter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator, Any
from copy import deepcopy
from rotor.schemas.request import ChatCompletionRequest
from rotor.core.exceptions import UpstreamOverloaded, is_overload_error_signal
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
        data = response.json()

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

        async for line in response.aiter_lines():
            line = line.strip()
            # Skip empty lines and heartbeat/comments (lines starting with ':')
            if not line or line.startswith(":"):
                continue

            # SSE format: lines start with "data: "
            if line.startswith("data: "):
                data_str = line[6:].strip()
                # Check for stream end marker
                if data_str == "[DONE]":
                    break

                try:
                    # Parse the JSON data
                    event = json.loads(data_str)
                    if event.get("type") == "error":
                        error = event.get("error") or {}
                        error_message = error.get("message") or "Anthropic upstream stream error"
                        error_type = error.get("type")
                        if is_overload_error_signal(error_type, error_message):
                            raise UpstreamOverloaded(
                                error_message, error_type=error_type
                            )
                        raise RuntimeError(error_message)

                    if request.anthropic_payload is not None:
                        yield event
                        continue

                    # Convert to OpenAI format
                    openai_chunk = ProtocolConverter.anthropic_stream_to_openai(
                        event,
                        request.model,
                        chunk_id
                    )

                    if openai_chunk:
                        yield openai_chunk

                except json.JSONDecodeError:
                    # Skip invalid JSON lines
                    continue
