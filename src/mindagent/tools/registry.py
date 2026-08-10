from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from mindagent.core import ActionRisk, AgentContext

from .base import BaseTool, ToolDefinition


class ToolNotFoundError(LookupError):
    pass


class ToolValidationError(ValueError):
    pass


class ToolRegistry:
    def __init__(self, tools: Iterable[BaseTool] | None = None):
        self._tools: dict[str, BaseTool] = {}
        self._closed = False
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        name = tool.definition.name
        if not name:
            raise ValueError("Tool name 不能为空")
        if name in self._tools:
            raise ValueError(f"Tool 已注册: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"Tool 不存在: {name}") from exc

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def provider_schemas(self) -> list[dict[str, Any]]:
        return [
            definition.to_provider_schema()
            for definition in self.definitions()
        ]

    def action_risks(self):
        return {
            definition.name: definition.risk
            for definition in self.definitions()
        }

    def approval_required(self) -> set[str]:
        return {
            definition.name
            for definition in self.definitions()
            if definition.require_human_approval
        }

    def assess_risk(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentContext | None = None,
    ) -> ActionRisk:
        return self.get(name).assess_risk(arguments, context)

    def validate_arguments(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        schema = self.get(name).definition.parameters
        if schema.get("type", "object") != "object":
            raise ToolValidationError("Tool parameters 根节点必须是 object")
        if not isinstance(arguments, dict):
            raise ToolValidationError("Tool arguments 必须是 object")

        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [key for key in required if key not in arguments]
        if missing:
            raise ToolValidationError(
                "缺少必填参数: " + ", ".join(missing)
            )

        if schema.get("additionalProperties") is False:
            unexpected = [key for key in arguments if key not in properties]
            if unexpected:
                raise ToolValidationError(
                    "包含未知参数: " + ", ".join(unexpected)
                )

        for key, value in arguments.items():
            property_schema = properties.get(key)
            if property_schema:
                self._validate_value(key, value, property_schema)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for tool in self._tools.values():
            if id(tool) in seen:
                continue
            seen.add(id(tool))
            close = getattr(tool, "close", None)
            if close is None:
                close = getattr(tool, "aclose", None)
            if not callable(close):
                continue
            result = close()
            if asyncio.iscoroutine(result):
                await result

    @classmethod
    def _validate_value(
        cls,
        path: str,
        value: Any,
        schema: dict[str, Any],
    ) -> None:
        expected = schema.get("type")
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
            "null": type(None),
        }
        python_type = type_map.get(expected)
        if python_type and (
            not isinstance(value, python_type)
            or expected in {"integer", "number"} and isinstance(value, bool)
        ):
            raise ToolValidationError(
                f"参数 {path} 类型错误，期望 {expected}"
            )

        if "enum" in schema and value not in schema["enum"]:
            raise ToolValidationError(f"参数 {path} 不在允许值中")

        if expected == "array" and "items" in schema:
            for index, item in enumerate(value):
                cls._validate_value(
                    f"{path}[{index}]",
                    item,
                    schema["items"],
                )
