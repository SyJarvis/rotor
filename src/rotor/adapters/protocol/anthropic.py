from rotor.adapters.base import AnthropicCompatibleAdapter
from rotor.adapters.protocol.converter import ProtocolConverter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator, Any
from rotor.schemas.request import ChatCompletionRequest
import json


class AnthropicAdapter(AnthropicCompatibleAdapter):
    """Adapter for Anthropic Claude API."""

    async def convert_request(self, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert OpenAI request to Anthropic format."""
        anthropic_request = ProtocolConverter.openai_to_anthropic(request)
        # Map model name if needed
        anthropic_request["model"] = self.map_model_name(request.model)
        return anthropic_request

    async def convert_response(self, response: Response, request: ChatCompletionRequest) -> dict[str, Any]:
        """Convert Anthropic response to OpenAI format."""
        data = response.json()

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


class AnthropicBedrockAdapter(AnthropicAdapter):
    """Adapter for Anthropic via AWS Bedrock."""

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for Bedrock Anthropic."""
        # Bedrock uses a different URL structure
        # Format: https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/invoke
        model = self.map_model_name(request.model)
        return f"{self.channel.base_url.rstrip('/')}/model/{model}/invoke"

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Bedrock requests."""
        # For Bedrock, we need AWS signature which would be handled differently
        # This is a simplified version
        return {
            "Content-Type": "application/json",
        }
