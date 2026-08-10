from __future__ import annotations

from typing import Any

from mindagent.core import ActionRisk

from ..base import BaseTool, ToolContext, ToolDefinition


class ContextQueryTool(BaseTool):
    definition = ToolDefinition(
        name="context_query",
        description="Read metadata or artifacts from the current AgentContext.",
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["metadata", "artifacts"],
                },
                "key": {"type": "string"},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        risk=ActionRisk.READ_ONLY,
    )

    async def execute(
        self,
        arguments: dict,
        context: ToolContext,
    ) -> Any:
        source_name = arguments["source"]
        source = getattr(context.agent_context, source_name)
        key = arguments.get("key")
        if key is None:
            return dict(source)
        if key not in source:
            raise KeyError(f"{source_name} 中不存在 key: {key}")
        return source[key]
