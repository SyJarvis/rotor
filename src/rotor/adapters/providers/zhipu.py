from rotor.adapters.base import OpenAICompatibleAdapter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator
from rotor.schemas.request import ChatCompletionRequest
import json


class ZhipuAdapter(OpenAICompatibleAdapter):
    """
    Adapter for Zhipu AI / BigModel API using OpenAI-compatible protocol.

    Zhipu AI OpenAI-compatible endpoint:
    - Base URL: https://open.bigmodel.cn/api/paas/v4
    - Uses Bearer token authentication
    - Supports streaming
    - Model names: glm-4, glm-4-plus, glm-4-flash, glm-4-air
    """

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for Zhipu OpenAI-compatible API."""
        return self.build_request_url("/chat/completions")

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Zhipu API requests."""
        return self.build_request_headers()

    async def convert_request(self, request: ChatCompletionRequest) -> dict:
        """Convert request to Zhipu OpenAI-compatible format."""
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

        # Zhipu-specific parameters
        if self.channel.extra:
            if "top_k" in self.channel.extra:
                body["top_k"] = self.channel.extra["top_k"]

        return body

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest
    ) -> AsyncIterator[dict]:
        """Convert Zhipu OpenAI-compatible streaming response."""
        async for line in response.aiter_lines():
            if line.strip() and line.startswith("data: "):
                data = line[6:]  # Remove "data: " prefix
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue
