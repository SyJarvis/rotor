from rotor.adapters.base import OpenAICompatibleAdapter
from rotor.adapters.protocol.anthropic import AnthropicAdapter
from rotor.channels.presets import join_api_url
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator
from copy import deepcopy
from rotor.schemas.request import ChatCompletionRequest


class ZhipuAdapter(OpenAICompatibleAdapter):
    """
    Adapter for Zhipu AI / BigModel API using OpenAI-compatible protocol.

    Zhipu AI OpenAI-compatible endpoint:
    - Base URL: https://open.bigmodel.cn/api/paas/v4
    - Uses Bearer token authentication
    - Supports streaming
    - Model names: glm-4, glm-4-plus, glm-4-flash, glm-4-air
    """

    native_anthropic = True

    async def count_tokens(self, body: dict, forwarded_headers: dict[str, str] | None = None) -> Response:
        payload = deepcopy(body)
        payload["model"] = self.map_model_name(str(payload["model"]))
        headers = self.build_request_headers()
        headers.update(forwarded_headers or {})
        base_url = (self.channel.extra or {}).get(
            "anthropic_base_url", "https://open.bigmodel.cn/api/anthropic/v1"
        )
        url = f"{join_api_url(str(base_url), '/messages', protocol='anthropic')}/count_tokens"
        response = await self.http_client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for Zhipu OpenAI-compatible API."""
        if request.anthropic_payload is not None:
            extra = self.channel.extra or {}
            base_url = extra.get(
                "anthropic_base_url",
                "https://open.bigmodel.cn/api/anthropic/v1",
            )
            return join_api_url(str(base_url), "/messages", protocol="anthropic")
        return self.build_request_url("/chat/completions")

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Zhipu API requests."""
        headers = self.build_request_headers()
        if request.anthropic_payload is not None:
            if request.stream:
                headers["accept"] = "text/event-stream"
            headers.update(request.anthropic_headers or {})
        return headers

    def prepare_request_body(
        self,
        request: ChatCompletionRequest,
        body: dict,
    ) -> dict:
        """Remove OpenAI stream options unsupported by Zhipu."""
        if request.anthropic_payload is not None:
            return body
        body = super().prepare_request_body(request, body)
        body.pop("stream_options", None)
        return body

    async def convert_request(self, request: ChatCompletionRequest) -> dict:
        """Convert request to Zhipu OpenAI-compatible format."""
        if request.anthropic_payload is not None:
            return await AnthropicAdapter.convert_request(self, request)

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

    async def convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest,
    ) -> dict:
        """Return native Anthropic messages without conversion."""
        if request.anthropic_payload is not None:
            return await AnthropicAdapter.convert_response(self, response, request)
        return await super().convert_response(response, request)

    async def stream_convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest
    ) -> AsyncIterator[dict]:
        """Convert Zhipu OpenAI-compatible streaming response."""
        if request.anthropic_payload is not None:
            async for event in AnthropicAdapter.stream_convert_response(
                self, response, request
            ):
                yield event
            return

        async for event in super().stream_convert_response(response, request):
            yield event
