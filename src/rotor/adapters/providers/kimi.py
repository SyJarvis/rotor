from rotor.adapters.base import OpenAICompatibleAdapter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator
from rotor.schemas.request import ChatCompletionRequest


class KimiAdapter(OpenAICompatibleAdapter):
    """
    Adapter for Kimi API (Moonshot AI) using OpenAI-compatible protocol.

    Kimi API is OpenAI-compatible:
    - Base URL: https://api.kimi.com/coding/v1
    - Uses standard Bearer token authentication
    - Supports streaming
    - Model names: kimi-k2.5, etc.
    """

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for Kimi API."""
        return self.build_request_url("/chat/completions")

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Kimi API requests."""
        return self.build_request_headers()

    async def convert_request(self, request: ChatCompletionRequest) -> dict:
        """Convert request to Kimi format."""
        mapped_model = self.map_model_name(request.model)

        body = {
            "model": mapped_model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": request.stream or False,
        }

        # Add optional parameters
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.stop is not None:
            body["stop"] = request.stop
        if request.tools is not None:
            body["tools"] = [t.model_dump(exclude_none=True) for t in request.tools]
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice

        return body

    async def convert_response(self, response: Response, request: ChatCompletionRequest) -> dict:
        """Convert Kimi OpenAI-compatible response."""
        data = await super().convert_response(response, request)

        if request.anthropic_payload is not None:
            from rotor.adapters.protocol.anthropic_integrity import validate_chat_response
            return validate_chat_response(data, secret=self.channel.key)

        return data

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest
    ) -> AsyncIterator[dict]:
        """Convert Kimi OpenAI-compatible streaming response."""
        async for event in super().stream_convert_response(response, request):
            yield event
