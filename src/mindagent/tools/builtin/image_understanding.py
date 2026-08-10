from __future__ import annotations

import hashlib
from typing import Any

from mindagent.core import ActionRisk
from mindagent.providers import ProviderRouter

from ..base import BaseTool, ToolContext, ToolDefinition


class ImageUnderstandingTool(BaseTool):
    definition = ToolDefinition(
        name="image_understanding",
        description="Analyze an image with a multimodal model.",
        parameters={
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "HTTP URL or base64 data URL of the image.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Instructions for analyzing the image.",
                },
                "detail": {
                    "type": "string",
                    "enum": ["low", "high", "auto"],
                },
                "api": {
                    "type": "string",
                    "enum": ["auto", "chat_completions", "responses"],
                },
                "model": {"type": "string"},
                "provider_name": {"type": "string"},
            },
            "required": ["image_url", "prompt"],
            "additionalProperties": False,
        },
        risk=ActionRisk.READ_ONLY,
    )

    def __init__(self, router: ProviderRouter):
        self.router = router

    async def execute(
        self,
        arguments: dict,
        context: ToolContext,
    ) -> dict[str, Any]:
        provider = self.router.select(
            provider_name=arguments.get("provider_name"),
            vision=True,
        )
        response = await provider.analyze_image(
            prompt=arguments["prompt"],
            image_url=arguments["image_url"],
            detail=arguments.get("detail"),
            api=arguments.get("api", "auto"),
            model=arguments.get("model"),
        )
        analysis = response.content or ""
        if not analysis.strip():
            raise ValueError("多模态 Provider 未返回分析结果")
        image_url = arguments["image_url"]
        image_id = hashlib.sha256(
            image_url.encode("utf-8")
        ).hexdigest()
        return {
            "analysis": analysis,
            "model": response.model,
            "usage": response.usage,
            "source": {
                "kind": "vision_model",
                "image_url": image_url,
                "prompt": arguments["prompt"],
                "model": response.model,
            },
            "artifact_refs": [
                {
                    "artifact_id": f"image:{image_id}",
                    "uri": image_url,
                    "media_type": "image/*",
                }
            ],
        }
