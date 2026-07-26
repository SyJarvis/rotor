from rotor.adapters.base import OpenAICompatibleAdapter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator
from rotor.schemas.request import ChatCompletionRequest
import json


class MiniMaxAdapter(OpenAICompatibleAdapter):
    """
    Adapter for MiniMax API using OpenAI-compatible protocol.

    MiniMax OpenAI-compatible endpoint:
    - Base URL: https://api.minimaxi.com/v1
    - Uses Bearer token authentication
    - Supports streaming
    - Model names: abab6.5s-chat, abab5.5-chat, MiniMax-M2.5
    """

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for MiniMax OpenAI-compatible API."""
        return self.build_request_url("/chat/completions")

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for MiniMax API requests."""
        return self.build_request_headers()

    async def convert_request(self, request: ChatCompletionRequest) -> dict:
        """Convert request to MiniMax format."""
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

        # MiniMax-specific parameters
        if self.channel.extra:
            if "bot_setting" in self.channel.extra:
                body["bot_setting"] = self.channel.extra["bot_setting"]
            if "mask_sensitive" in self.channel.extra:
                body["mask_sensitive"] = self.channel.extra["mask_sensitive"]

        return body

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest
    ) -> AsyncIterator[dict]:
        """Convert MiniMax OpenAI-compatible streaming response."""
        async for line in response.aiter_lines():
            if line.strip() and line.startswith("data: "):
                data = line[6:]  # Remove "data: " prefix
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue
