from rotor.adapters.base import OpenAICompatibleAdapter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator
from rotor.schemas.request import ChatCompletionRequest
import json


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
        return f"{self.channel.base_url.rstrip('/')}/chat/completions"

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Kimi API requests."""
        headers = {
            "Authorization": f"Bearer {self.channel.key}",
            "Content-Type": "application/json",
        }

        # Kimi may require additional headers
        if self.channel.extra and "headers" in self.channel.extra:
            headers.update(self.channel.extra["headers"])

        return headers

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

        return body

    async def convert_response(self, response: Response, request: ChatCompletionRequest) -> dict:
        """Convert Kimi OpenAI-compatible response."""
        data = response.json()

        # Kimi should return OpenAI-compatible format
        # Ensure the response has the expected structure
        if "choices" not in data:
            # Handle edge case where response might be different
            return {
                "id": data.get("id", "chatcmpl-unknown"),
                "object": "chat.completion",
                "created": data.get("created", 0),
                "model": data.get("model", request.model),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": data.get("content", data.get("message", "")),
                        },
                        "finish_reason": data.get("finish_reason", "stop"),
                    }
                ],
                "usage": data.get("usage", {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                })
            }

        return data

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest
    ) -> AsyncIterator[dict]:
        """Convert Kimi OpenAI-compatible streaming response."""
        async for line in response.aiter_lines():
            if line.strip() and line.startswith("data: "):
                data = line[6:]  # Remove "data: " prefix
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue
