"""Validate complete provider results and reconstruct native Anthropic messages."""

from copy import deepcopy
import json

from rotor.core.exceptions import UpstreamOverloaded, UpstreamProtocolError, is_overload_error_signal


def reject_provider_error(payload, *, secret=None):
    if not isinstance(payload, dict):
        raise UpstreamProtocolError("Upstream returned a non-object response")
    if payload.get("error") is not None or payload.get("type") == "error" or payload.get("success") is False:
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        kind = error.get("type")
        message = str(error.get("message") or "Upstream returned an error response")
        if secret:
            message = message.replace(secret, "[redacted]")
        if is_overload_error_signal(kind, message):
            raise UpstreamOverloaded(message, error_type=kind)
        raise UpstreamProtocolError(message)


def _require(condition, message):
    if not condition:
        raise UpstreamProtocolError(message)


def _validate_block(block, *, final=True):
    _require(isinstance(block, dict) and isinstance(block.get("type"), str) and bool(block["type"]), "Invalid Anthropic content block")
    kind = block["type"]
    if kind == "text":
        _require(isinstance(block.get("text"), str), "Invalid Anthropic text block")
    elif kind == "thinking":
        _require(isinstance(block.get("thinking"), str), "Invalid Anthropic thinking block")
        if final:
            _require(isinstance(block.get("signature"), str), "Missing Anthropic thinking signature")
    elif kind in {"tool_use", "server_tool_use"}:
        _require(isinstance(block.get("id"), str) and bool(block["id"])
                 and isinstance(block.get("name"), str) and bool(block["name"]), "Invalid Anthropic tool block")
        _require(isinstance(block.get("input"), dict), "Anthropic tool input must be an object")


def validate_native_message(message, *, final=True, secret=None):
    reject_provider_error(message, secret=secret)
    _require(message.get("type") == "message" and message.get("role") == "assistant"
             and isinstance(message.get("id"), str) and bool(message["id"])
             and isinstance(message.get("model"), str) and bool(message["model"])
             and isinstance(message.get("content"), list), "Invalid Anthropic message response")
    _require(isinstance(message.get("usage"), dict), "Missing Anthropic message usage")
    for name in ("input_tokens", "output_tokens"):
        value = message["usage"].get(name)
        _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0, "Invalid Anthropic message usage")
    if final:
        _require(isinstance(message.get("stop_reason"), str) and bool(message["stop_reason"]), "Missing Anthropic stop reason")
    for block in message["content"]:
        _validate_block(block, final=final)
    return message


def validate_chat_response(response, *, secret=None):
    reject_provider_error(response, secret=secret)
    choices = response.get("choices")
    _require(isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict), "Invalid converted completion choices")
    choice = choices[0]
    message = choice.get("message")
    _require(isinstance(message, dict) and message.get("role") == "assistant", "Invalid converted completion message")
    _require(isinstance(choice.get("finish_reason"), str) and bool(choice["finish_reason"]), "Missing converted completion stop reason")
    for tool in message.get("tool_calls") or []:
        _require(isinstance(tool, dict) and isinstance(tool.get("function"), dict), "Invalid converted tool call")
        _require(isinstance(tool.get("id"), str) and bool(tool["id"])
                 and isinstance(tool["function"].get("name"), str) and bool(tool["function"]["name"]),
                 "Missing converted tool id or name")
        arguments = tool["function"].get("arguments")
        try:
            value = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (ValueError, TypeError):
            raise UpstreamProtocolError("Invalid converted tool input JSON") from None
        _require(isinstance(value, dict), "Converted tool input must be an object")
    return response


class ChatStreamIntegrity:
    def __init__(self, *, secret=None):
        self.secret = secret
        self.finish_reason = None
        self.tools = {}

    def feed(self, chunk):
        reject_provider_error(chunk, secret=self.secret)
        choices = chunk.get("choices") or []
        _require(isinstance(choices, list) and len(choices) <= 1, "Invalid converted stream choices")
        if not choices:
            return
        choice = choices[0]
        _require(isinstance(choice, dict), "Invalid converted stream choice")
        delta = choice.get("delta") or {}
        _require(isinstance(delta, dict), "Invalid converted stream delta")
        _require(self.finish_reason is None or not delta, "Content after converted stream termination")
        for tool in delta.get("tool_calls") or []:
            _require(isinstance(tool, dict) and isinstance(tool.get("index"), int), "Invalid converted tool index")
            state = self.tools.setdefault(tool["index"], {"id": "", "name": "", "arguments": ""})
            function = tool.get("function") or {}
            _require(isinstance(function, dict), "Invalid converted tool function")
            if tool.get("id"):
                state["id"] = tool["id"]
            if function.get("name"):
                state["name"] = function["name"]
            if "arguments" in function:
                _require(isinstance(function["arguments"], str), "Invalid converted tool argument delta")
                state["arguments"] += function["arguments"]
        if choice.get("finish_reason") is not None:
            _require(isinstance(choice["finish_reason"], str) and bool(choice["finish_reason"]), "Invalid converted stop reason")
            self.finish_reason = choice["finish_reason"]

    def finish(self):
        _require(self.finish_reason is not None, "Converted stream ended without a stop reason")
        for tool in self.tools.values():
            _require(bool(tool["id"]) and bool(tool["name"]), "Incomplete converted tool call")
            try:
                value = json.loads(tool["arguments"])
            except (ValueError, TypeError):
                raise UpstreamProtocolError("Truncated converted tool input JSON") from None
            _require(isinstance(value, dict), "Converted tool input must be an object")


class NativeMessageStream:
    def __init__(self, *, secret=None):
        self.secret = secret
        self.message = None
        self.blocks = {}
        self.open_blocks = set()
        self.tool_json = {}
        self.stopped = False
        self.saw_message_delta = False
        self.extensions = []

    def feed(self, event):
        reject_provider_error(event, secret=self.secret)
        kind = event.get("type")
        _require(isinstance(kind, str) and bool(kind), "Missing Anthropic event type")
        if kind == "ping":
            return
        if kind == "message_start":
            _require(self.message is None and not self.stopped, "Duplicate Anthropic message_start")
            self.message = deepcopy(validate_native_message(event.get("message"), final=False, secret=self.secret))
            _require(self.message.get("stop_reason") is None, "Anthropic message_start already has a stop reason")
            self.blocks = {index: deepcopy(block) for index, block in enumerate(self.message["content"])}
            return
        known = {"content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"}
        if kind not in known:
            self.extensions.append(deepcopy(event))
            return
        _require(self.message is not None and not self.stopped, "Anthropic event outside a message lifecycle")
        if kind.startswith("content_block_"):
            index = event.get("index")
            _require(isinstance(index, int) and not isinstance(index, bool) and index >= 0, "Invalid Anthropic content index")
            if kind == "content_block_start":
                _require(index == len(self.blocks), "Duplicate or non-contiguous Anthropic content index")
                block = deepcopy(event.get("content_block"))
                _validate_block(block, final=False)
                self.blocks[index] = block
                self.open_blocks.add(index)
            else:
                _require(index in self.open_blocks, "Anthropic content event has no open block")
                if kind == "content_block_stop":
                    if index in self.tool_json:
                        try:
                            self.blocks[index]["input"] = json.loads(self.tool_json[index])
                        except (ValueError, TypeError):
                            raise UpstreamProtocolError("Truncated Anthropic tool input JSON") from None
                    _validate_block(self.blocks[index])
                    self.open_blocks.remove(index)
                else:
                    self._delta(index, event)
        elif kind == "message_delta":
            _require(not self.open_blocks, "Anthropic message_delta before content block completion")
            delta = event.get("delta")
            _require(isinstance(delta, dict), "Invalid Anthropic message delta")
            self.message.update(deepcopy(delta))
            usage = event.get("usage")
            _require(isinstance(usage, dict), "Missing Anthropic message delta usage")
            self.message["usage"].update(deepcopy(usage))
            self.saw_message_delta = True
        elif kind == "message_stop":
            _require(not self.open_blocks, "Anthropic message stopped with incomplete content blocks")
            self.stopped = True

    def _delta(self, index, event):
        block = self.blocks[index]
        delta = event.get("delta")
        _require(isinstance(delta, dict), "Invalid Anthropic content delta")
        kind = delta.get("type")
        _require(isinstance(kind, str) and bool(kind), "Missing Anthropic content delta type")
        fields = {"text_delta": ("text", "text"), "thinking_delta": ("thinking", "thinking"),
                  "signature_delta": ("thinking", "signature")}
        if kind in fields:
            block_type, field = fields[kind]
            _require(block["type"] == block_type and isinstance(delta.get(field), str), "Mismatched Anthropic content delta")
            block[field] = block.get(field, "") + delta[field]
        elif kind == "input_json_delta":
            _require(block["type"] in {"tool_use", "server_tool_use"} and isinstance(delta.get("partial_json"), str), "Invalid Anthropic tool JSON delta")
            self.tool_json[index] = self.tool_json.get(index, "") + delta["partial_json"]
        elif kind == "citations_delta":
            block.setdefault("citations", []).append(deepcopy(delta.get("citation")))
        else:
            # Unknown extensions still flow to the client; retain their raw
            # events in the archive rather than inventing merge semantics.
            self.extensions.append(deepcopy(event))

    def finish(self):
        _require(self.message is not None and self.stopped and self.saw_message_delta,
                 "Anthropic stream ended without message_start/message_delta/message_stop")
        self.message["content"] = [self.blocks[index] for index in sorted(self.blocks)]
        validate_native_message(self.message, secret=self.secret)
        if self.extensions:
            self.message["stream_extensions"] = deepcopy(self.extensions)
        return deepcopy(self.message)


async def iter_anthropic_sse(response, *, secret=None, allow_done=False):
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type and content_type != "text/event-stream":
        await response.aread()
        try:
            reject_provider_error(response.json(), secret=secret)
        except ValueError:
            pass
        raise UpstreamProtocolError("Upstream did not return an Anthropic event stream")
    data = []
    event_name = None

    def decode():
        try:
            event = json.loads("\n".join(data))
        except (ValueError, TypeError):
            raise UpstreamProtocolError("Invalid JSON in Anthropic event stream") from None
        _require(isinstance(event, dict), "Invalid Anthropic SSE event")
        known_events = {"message_start", "message_delta", "message_stop", "content_block_start",
                        "content_block_delta", "content_block_stop", "ping", "error"}
        if event_name in known_events and event.get("type") is not None:
            _require(event["type"] == event_name, "Conflicting Anthropic SSE event types")
        if event_name and "type" not in event:
            event["type"] = event_name
        return event

    async for line in response.aiter_lines():
        if not line:
            if allow_done and "\n".join(data).strip() == "[DONE]":
                return
            if data:
                yield decode()
            data, event_name = [], None
        elif not line.startswith(":"):
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
            elif field == "event":
                event_name = value
    if data and not (allow_done and "\n".join(data).strip() == "[DONE]"):
        yield decode()
