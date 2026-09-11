"""Minimal Chat response framing and completion checks for compatible providers."""

from copy import deepcopy
import json

from rotor.core.exceptions import UpstreamOverloaded, UpstreamProtocolError, is_overload_error_signal


_TOKEN_FIELDS = {
    "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "cached_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
}
_DETAIL_FIELDS = {"prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details", "cache_creation"}
_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "function_call"}
_MESSAGE_FIELDS = {"content", "tool_calls", "function_call", "refusal", "audio", "reasoning_content"}


def _provider_usage(payload, *, strict=False, upstream_status=None):
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if usage is None:
        return None
    if not isinstance(usage, dict):
        if strict:
            _fail("Invalid Chat token usage", upstream_status=upstream_status)
        return None
    result = {}
    for name, value in usage.items():
        if name in _TOKEN_FIELDS:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                if strict:
                    _fail("Invalid Chat token usage", upstream_status=upstream_status)
                return None
            result[name] = value
        elif name in _DETAIL_FIELDS:
            if value is None:
                continue
            if not isinstance(value, dict) or any(
                count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0)
                for count in value.values()
            ):
                if strict:
                    _fail("Invalid Chat token usage details", upstream_status=upstream_status)
                return None
            result[name] = {detail: count for detail, count in value.items() if count is not None}
    return result if any(name in result for name in _TOKEN_FIELDS) else None


def _fail(message, *, upstream_status=None, payload=None, usage=None):
    error = UpstreamProtocolError(message)
    error.upstream_status = upstream_status
    provider_usage = _provider_usage(payload) if payload is not None else usage
    if provider_usage is not None:
        error.provider_usage = deepcopy(provider_usage)
    raise error


def reject_chat_error(payload, *, upstream_status=None, secret=None):
    if not isinstance(payload, dict):
        _fail("Upstream returned a non-object Chat response", upstream_status=upstream_status)
    if payload.get("error") is not None or payload.get("type") == "error" or payload.get("success") is False:
        error = payload.get("error")
        details = error if isinstance(error, dict) else payload
        kind = details.get("code") if details.get("type") == "error" else details.get("type") or details.get("code")
        kind = str(kind) if kind is not None else None
        message = details.get("message") or details.get("msg")
        if isinstance(error, str):
            message = error
        message = str(message or "Upstream returned a Chat error response")
        if secret:
            message = message.replace(secret, "[redacted]")
            kind = kind.replace(secret, "[redacted]") if kind else kind
        if is_overload_error_signal(kind, message):
            overloaded = UpstreamOverloaded(message, error_type=kind)
            overloaded.upstream_status = upstream_status
            usage = _provider_usage(payload)
            if usage is not None:
                overloaded.provider_usage = usage
            raise overloaded
        _fail(message, upstream_status=upstream_status, payload=payload)


def validate_chat_response(payload, *, expected_choices=None, upstream_status=None, secret=None):
    reject_chat_error(payload, upstream_status=upstream_status, secret=secret)
    _provider_usage(payload, strict=True, upstream_status=upstream_status)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        _fail("Chat completion has no choices", upstream_status=upstream_status, payload=payload)
    if expected_choices is not None and len(choices) != expected_choices:
        _fail("Chat completion has an unexpected choice count", upstream_status=upstream_status, payload=payload)
    indices = set()
    for position, choice in enumerate(choices):
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            _fail("Invalid Chat completion message", upstream_status=upstream_status, payload=payload)
        message = choice["message"]
        if not _MESSAGE_FIELDS.intersection(message) or ("role" in message and message["role"] != "assistant"):
            _fail("Invalid Chat completion assistant message", upstream_status=upstream_status, payload=payload)
        index = choice.get("index", position)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index in indices:
            _fail("Invalid Chat completion choice index", upstream_status=upstream_status, payload=payload)
        indices.add(index)
        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str) or finish_reason not in _FINISH_REASONS:
            _fail("Chat completion has a missing or unsupported finish reason", upstream_status=upstream_status, payload=payload)
    return payload


class ChatStreamIntegrity:
    def __init__(self, *, expected_choices=None, upstream_status=None, secret=None):
        self.expected_choices = expected_choices
        self.upstream_status = upstream_status
        self.secret = secret
        self.choices = {}
        self.provider_usage = None

    def _fail(self, message):
        _fail(message, upstream_status=self.upstream_status, usage=self.provider_usage)

    def feed(self, chunk):
        try:
            reject_chat_error(chunk, upstream_status=self.upstream_status, secret=self.secret)
        except (UpstreamProtocolError, UpstreamOverloaded) as error:
            if not hasattr(error, "provider_usage") and self.provider_usage is not None:
                error.provider_usage = deepcopy(self.provider_usage)
            raise
        usage = _provider_usage(chunk, strict=True, upstream_status=self.upstream_status)
        if usage is not None:
            self.provider_usage = usage
        choices = chunk.get("choices")
        if choices is None and isinstance(chunk.get("usage"), dict):
            choices = []
        if not isinstance(choices, list):
            self._fail("Invalid Chat stream choices")
        seen = set()
        for choice in choices:
            if not isinstance(choice, dict):
                self._fail("Invalid Chat stream choice")
            index = choice.get("index", 0 if len(choices) == 1 else None)
            if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index in seen:
                self._fail("Invalid Chat stream choice index")
            seen.add(index)
            delta = choice.get("delta")
            if delta is None:
                delta = {}
            if not isinstance(delta, dict):
                self._fail("Invalid Chat stream delta")
            if self.choices.get(index) is not None and any(value not in (None, "", [], {}) for value in delta.values()):
                self._fail("Chat content followed a choice finish reason")
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str) or finish_reason not in _FINISH_REASONS:
                    self._fail("Invalid Chat stream finish reason")
                if self.choices.get(index) not in (None, finish_reason):
                    self._fail("Conflicting Chat stream finish reasons")
                self.choices[index] = finish_reason
            else:
                self.choices.setdefault(index, None)

    def finish(self):
        if not self.choices or any(reason is None for reason in self.choices.values()):
            self._fail("Chat stream ended without all choice finish reasons")
        if self.expected_choices is not None and set(self.choices) != set(range(self.expected_choices)):
            self._fail("Chat stream ended with an unexpected choice count")


async def iter_chat_sse(response, *, secret=None):
    """Parse raw SSE strictly; completion/empty-retry policy belongs to callers."""
    status = response.status_code
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type and content_type != "text/event-stream":
        await response.aread()
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            _fail("Upstream did not return a Chat event stream", upstream_status=status)
        reject_chat_error(payload, upstream_status=status, secret=secret)
        _fail("Upstream did not return a Chat event stream", upstream_status=status, payload=payload)

    data = []
    event_name = None
    done = False

    def decode():
        nonlocal done
        raw = "\n".join(data)
        if done:
            _fail("Chat stream contains data after [DONE]", upstream_status=status)
        if raw.strip() == "[DONE]":
            if event_name == "error":
                _fail("Upstream returned a Chat error event", upstream_status=status)
            done = True
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            _fail("Invalid JSON in Chat event stream", upstream_status=status)
        if event_name == "error":
            envelope = {"type": "error", "error": payload}
            if isinstance(payload, dict):
                envelope["error"] = payload.get("error") or payload
                envelope["usage"] = payload.get("usage")
            reject_chat_error(envelope, upstream_status=status, secret=secret)
        reject_chat_error(payload, upstream_status=status, secret=secret)
        return payload

    async for line in response.aiter_lines():
        if not line:
            if data:
                payload = decode()
                if payload is not None:
                    yield payload
            data, event_name = [], None
        elif not line.startswith(":"):
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
            elif field == "event":
                event_name = value
    if data:
        payload = decode()
        if payload is not None:
            yield payload
