from __future__ import annotations

from typing import Any

from mindagent.core import ActionRisk

from ..base import BaseTool, ToolContext, ToolDefinition


class MemoryTool(BaseTool):
    definition = ToolDefinition(
        name="memory",
        description=(
            "Read or write process-local memory scoped to the current session."
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["get", "set"],
                },
                "key": {"type": "string"},
                "value": {},
            },
            "required": ["operation", "key"],
            "additionalProperties": False,
        },
        risk=ActionRisk.WRITE,
        require_human_approval=False,
    )

    def __init__(self):
        self._namespaces: dict[str, dict[str, Any]] = {}

    async def execute(
        self,
        arguments: dict,
        context: ToolContext,
    ) -> dict[str, Any]:
        environment = context.environment
        namespace = (
            environment.memory_namespace
            if environment is not None
            and environment.memory_namespace is not None
            else context.session_id or context.run_id
        )
        memory = self._namespaces.setdefault(namespace, {})
        key = arguments["key"]
        if arguments["operation"] == "set":
            if "value" not in arguments:
                raise ValueError("memory set 操作需要 value")
            memory[key] = arguments["value"]
            return {"key": key, "value": memory[key], "stored": True}

        return {
            "key": key,
            "value": memory.get(key),
            "found": key in memory,
        }
