from rotor.adapters.base import OpenAICompatibleAdapter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator
from rotor.schemas.request import ChatCompletionRequest


class MoonshotAdapter(OpenAICompatibleAdapter):
    """
    Adapter for Moonshot/Kimi API.

    Moonshot API is OpenAI-compatible with some differences:
    - Base URL: https://api.moonshot.cn/v1
    - Uses standard Bearer token authentication
    - Supports streaming
    - Model names: moonshot-v1-8k, moonshot-v1-32k, moonshot-v1-128k
    """

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for Moonshot API."""
        return f"{self.channel.base_url.rstrip('/')}/chat/completions"

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Moonshot API requests."""
        headers = {
            "Authorization": f"Bearer {self.channel.key}",
            "Content-Type": "application/json",
        }

        # Moonshot may require additional headers
        if extra := self.channel.extra.get("headers"):
            headers.update(extra)

        return headers

    async def convert_request(self, request: ChatCompletionRequest) -> dict:
        """Convert request to Moonshot format."""
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
        if request.n is not None:
            body["n"] = request.n
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.presence_penalty is not None:
            body["presence_penalty"] = request.presence_penalty
        if request.frequency_penalty is not None:
            body["frequency_penalty"] = request.frequency_penalty
        if request.stop is not None:
            body["stop"] = request.stop
        if request.tools is not None:
            body["tools"] = [t.model_dump(exclude_none=True) for t in request.tools]
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.user is not None:
            body["user"] = request.user

        # Moonshot-specific parameters from extra config
        if "enable_search" in self.channel.extra:
            body["enable_search"] = self.channel.extra["enable_search"]

        return body
