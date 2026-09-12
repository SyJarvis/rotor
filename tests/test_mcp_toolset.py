import asyncio
from types import SimpleNamespace

import pytest

from rotor.mcp_toolset import (
    MCPConnectionError,
    MCPTool,
    MCPToolCallError,
    MCPToolSet,
)


class FakeRemoteTool:
    def __init__(self, name: str, schema: dict | None = None) -> None:
        self.name = name
        self.description = f"{name} description"
        self.input_schema = schema or {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }


class FakeCallResult:
    def __init__(self, *, is_error=False, structured=None, content=None) -> None:
        self.is_error = is_error
        self.structured_content = structured
        self.content = content or []


class FakeClient:
    """Orders tools once per connection and records every call."""

    def __init__(self, tools, *, pages=None, result=None) -> None:
        self._tools = tools
        self._pages = pages
        self._result = result or FakeCallResult(structured={"ok": True})
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def list_tools(self, cursor=None):
        if self._pages is None:
            return SimpleNamespace(tools=self._tools, next_cursor=None)
        index = 0 if cursor is None else int(cursor)
        page = self._pages[index]
        next_cursor = str(index + 1) if index + 1 < len(self._pages) else None
        return SimpleNamespace(tools=page, next_cursor=next_cursor)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._result


class FakeContext:
    def __init__(self, client) -> None:
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc_info):
        self.client.closed = True
        return False


def _tool_set(client, allowed) -> MCPToolSet:
    return MCPToolSet(FakeContext(client), allowed_tools=set(allowed))


def test_open_keeps_only_allowlisted_tools_in_sorted_order():
    client = FakeClient([
        FakeRemoteTool("rotor_list_channels"),
        FakeRemoteTool("rotor_delete_everything"),
        FakeRemoteTool("rotor_get_channel"),
    ])
    tool_set = _tool_set(client, {"rotor_list_channels", "rotor_get_channel"})

    tools = asyncio.run(tool_set.open())

    assert [tool.definition.name for tool in tools] == [
        "rotor_get_channel",
        "rotor_list_channels",
    ]
    assert client.calls == []


def test_open_fails_when_the_server_is_missing_an_allowlisted_tool():
    client = FakeClient([FakeRemoteTool("rotor_list_channels")])
    tool_set = _tool_set(client, {"rotor_list_channels", "rotor_get_channel"})

    with pytest.raises(MCPConnectionError) as error:
        asyncio.run(tool_set.open())

    assert "rotor_get_channel" in str(error.value)
    # A failed open must not leak the child process connection.
    assert client.closed is True


def test_open_follows_pagination_cursors():
    client = FakeClient(
        [],
        pages=[
            [FakeRemoteTool("rotor_get_channel")],
            [FakeRemoteTool("rotor_list_channels")],
        ],
    )
    tool_set = _tool_set(client, {"rotor_get_channel", "rotor_list_channels"})

    tools = asyncio.run(tool_set.open())

    assert [tool.definition.name for tool in tools] == [
        "rotor_get_channel",
        "rotor_list_channels",
    ]


def test_open_rejects_a_second_connection_attempt():
    client = FakeClient([FakeRemoteTool("rotor_list_channels")])
    tool_set = _tool_set(client, {"rotor_list_channels"})
    asyncio.run(tool_set.open())

    with pytest.raises(MCPConnectionError):
        asyncio.run(tool_set.open())


def test_tool_execution_returns_structured_content():
    client = FakeClient(
        [FakeRemoteTool("rotor_get_request_trace")],
        result=FakeCallResult(structured={"data": {"request_id": "req-1"}}),
    )
    tool_set = _tool_set(client, {"rotor_get_request_trace"})

    async def run():
        tool = (await tool_set.open())[0]
        return await tool.execute({"request_id": "req-1"}, object())

    assert asyncio.run(run()) == {"data": {"request_id": "req-1"}}
    assert client.calls == [("rotor_get_request_trace", {"request_id": "req-1"})]


def test_tool_execution_falls_back_to_content_blocks():
    block = SimpleNamespace(model_dump=lambda **kwargs: {"type": "text", "text": "ok"})
    client = FakeClient(
        [FakeRemoteTool("rotor_list_channels")],
        result=FakeCallResult(content=[block]),
    )
    tool_set = _tool_set(client, {"rotor_list_channels"})

    async def run():
        tool = (await tool_set.open())[0]
        return await tool.execute({}, object())

    assert asyncio.run(run()) == {"content": [{"type": "text", "text": "ok"}]}


def test_remote_tool_failure_surfaces_the_server_message():
    client = FakeClient(
        [FakeRemoteTool("rotor_get_channel")],
        result=FakeCallResult(
            is_error=True,
            content=[SimpleNamespace(text="channel 7 not found")],
        ),
    )
    tool_set = _tool_set(client, {"rotor_get_channel"})

    async def run():
        tool = (await tool_set.open())[0]
        return await tool.execute({"channel_id": 7}, object())

    with pytest.raises(MCPToolCallError) as error:
        asyncio.run(run())

    assert "rotor_get_channel" in str(error.value)
    assert "channel 7 not found" in str(error.value)


def test_calls_after_close_are_rejected():
    client = FakeClient([FakeRemoteTool("rotor_list_channels")])
    tool_set = _tool_set(client, {"rotor_list_channels"})

    async def run():
        await tool_set.open()
        await tool_set.close()
        await tool_set.call_tool("rotor_list_channels", {})

    with pytest.raises(MCPConnectionError):
        asyncio.run(run())


def test_registry_close_closes_the_connection_once():
    from mindagent.tools import ToolRegistry

    client = FakeClient([FakeRemoteTool("rotor_list_channels")])
    tool_set = _tool_set(client, {"rotor_list_channels"})

    async def run():
        registry = ToolRegistry(await tool_set.open())
        await registry.close()
        await registry.close()

    asyncio.run(run())

    assert client.closed is True


def test_remote_tool_definitions_are_read_only_by_default():
    from mindagent.core import ActionRisk

    client = FakeClient([FakeRemoteTool("rotor_list_channels")])
    tool_set = _tool_set(client, {"rotor_list_channels"})

    async def run():
        return await tool_set.open()

    tool = asyncio.run(run())[0]

    assert isinstance(tool, MCPTool)
    assert tool.definition.risk == ActionRisk.READ_ONLY
    assert tool.definition.parameters["type"] == "object"


def test_stdio_without_the_mcp_sdk_reports_the_extra(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "mcp":
            raise ImportError("no mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(MCPConnectionError) as error:
        MCPToolSet.stdio(
            command="rotor-mcp",
            allowed_tools={"rotor_list_channels"},
        )

    assert "rotor-gateway[mcp]" in str(error.value)
