from rotor.adapters.base import OpenAICompatibleAdapter
from rotor.models.channel import Channel
from httpx import AsyncClient, Response
from typing import AsyncIterator
from rotor.schemas.request import ChatCompletionRequest


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Adapter for OpenAI API."""

    pass  # Uses the default OpenAI-compatible implementation


class AzureOpenAIAdapter(OpenAICompatibleAdapter):
    """Adapter for Azure OpenAI API."""

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        """Get the target URL for Azure OpenAI."""
        # Azure OpenAI uses a different URL structure
        # base_url should be like: https://your-resource.openai.azure.com/openai/deployments/your-deployment
        deployment = self.map_model_name(request.model)
        return f"{self.channel.base_url.rstrip('/')}/chat/completions?api-version=2023-05-15"

    def setup_request_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        """Set up headers for Azure OpenAI requests."""
        return {
            "api-key": self.channel.key,
            "Content-Type": "application/json",
        }
