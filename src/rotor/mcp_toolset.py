"""MCP client adapter that exposes an allowlisted server tool set.

Rotor starts the Rotor MCP Server as a stdio child process and hands the
resulting tools to MindAgent.  The adapter owns the connection for the whole
chat run and closes it when the registry is closed.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mindagent.tools import BaseTool, ToolContext, ToolDefinition


class MCPConnectionError(RuntimeError):
    """The MCP client or server connection could not be established."""


class MCPToolCallError(RuntimeError):
    """A remote MCP tool reported a failure."""


class MCPToolSet:
    """Own one MCP client connection and expose an allowlisted tool set."""

    def __init__(
        self,
        client_context: Any,
        *,
        allowed_tools: set[str],
    ) -> None:
        if not allowed_tools:
            raise ValueError("allowed_tools 不能为空")
        self._client_context = client_context
        self._allowed_tools = allowed_tools
        self._stack = AsyncExitStack()
        self._client: Any | None = None
        self._closed = False

    @classmethod
    def stdio(
        cls,
        *,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        allowed_tools: set[str],
    ) -> "MCPToolSet":
        try:
            from mcp import Client, StdioServerParameters, stdio_client
        except (ImportError, AttributeError) as exc:
            raise MCPConnectionError(
                "MCP 客户端未安装；请安装 rotor-gateway[mcp]"
            ) from exc

        parameters = StdioServerParameters(
            command=command,
            args=args or [],
            env=env,
            cwd=cwd,
        )
        return cls(
            Client(stdio_client(parameters), read_timeout_seconds=30),
            allowed_tools=allowed_tools,
        )

    async def open(self) -> list["MCPTool"]:
        if self._client is not None:
            raise MCPConnectionError("MCP ToolSet 已连接")
        try:
            self._client = await self._stack.enter_async_context(
                self._client_context
            )
            remote_tools = await self._list_all_tools()
            selected = {
                tool.name: tool
                for tool in remote_tools
                if tool.name in self._allowed_tools
            }
            missing = self._allowed_tools - set(selected)
            if missing:
                raise MCPConnectionError(
                    "MCP Server 缺少允许的工具: "
                    + ", ".join(sorted(missing))
                )
            return [
                MCPTool(owner=self, remote_tool=selected[name])
                for name in sorted(selected)
            ]
        except Exception:
            await self.close()
            raise

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        if self._client is None or self._closed:
            raise MCPConnectionError("MCP ToolSet 未连接")
        result = await self._client.call_tool(name, arguments)
        if result.is_error:
            message = self._error_text(result.content)
            raise MCPToolCallError(f"MCP tool {name} failed: {message}")
        if result.structured_content is not None:
            return result.structured_content
        return {
            "content": [
                block.model_dump(mode="json", by_alias=True)
                for block in result.content
            ]
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client = None
        await self._stack.aclose()

    async def _list_all_tools(self) -> list[Any]:
        assert self._client is not None
        tools: list[Any] = []
        cursor: str | None = None
        while True:
            result = await self._client.list_tools(cursor=cursor)
            tools.extend(result.tools)
            cursor = result.next_cursor
            if cursor is None:
                return tools

    @staticmethod
    def _error_text(content: list[Any]) -> str:
        text = "Tool execution failed"
        for block in content:
            candidate = getattr(block, "text", None)
            if isinstance(candidate, str) and candidate:
                text = candidate
                break
        return text[:500]


class MCPTool(BaseTool):
    def __init__(self, *, owner: MCPToolSet, remote_tool: Any) -> None:
        self._owner = owner
        self.definition = ToolDefinition(
            name=remote_tool.name,
            description=remote_tool.description or "",
            parameters=remote_tool.input_schema,
        )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> Any:
        return await self._owner.call_tool(self.definition.name, arguments)

    async def close(self) -> None:
        await self._owner.close()
