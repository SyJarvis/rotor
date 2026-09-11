"""Bounded inspection of oversized Responses SSE JSON objects."""
import codecs
import json
import re


_STRING_SPECIAL = re.compile(rb'["\\\x00-\x1f]')
_SCALAR_END = re.compile(rb'[\s,\]}]')
_LONG_STRING = object()


class ResponseEventType:
    """Retain only root type/error, with bounded tokens and nesting.

    Ordinary events use json.loads. This fallback validates JSON structure while
    skipping long string contents, without retaining response text or arguments.
    """

    def __init__(self):
        self.event_type = ""
        self.error = False
        self.invalid = False
        self.complete = False
        self._stack = []
        self._key = None
        self._token = bytearray()
        self._string = False
        self._long = False
        self._escape = False
        self._unicode = 0
        self._scalar = False
        self._utf8 = codecs.getincrementaldecoder("utf-8")()

    def _accept(self, kind, value):
        if not self._stack:
            self.complete = kind == "object"
            self.invalid = not self.complete
            return
        frame = self._stack[-1]
        state = frame[1]
        if state in {"key", "key_or_end"} and kind == "string":
            if len(self._stack) == 1:
                self._key = value
            frame[1] = "colon"
        elif state in {"value", "value_or_end"}:
            if len(self._stack) == 1 and frame[0] == "object":
                if self._key == "type":
                    self.event_type = value if kind == "string" else (_LONG_STRING if value else "")
                elif self._key == "error":
                    self.error = bool(value)
            frame[1] = "comma_or_end"
            frame[2] = True
        else:
            self.invalid = True

    def feed(self, data: bytes):
        if self.invalid:
            return
        try:
            for start in range(0, len(data), 4096):
                self._utf8.decode(data[start:start + 4096])
        except UnicodeDecodeError:
            self.invalid = True
            return
        index = 0
        while index < len(data) and not self.invalid:
            if self._string:
                if self._unicode:
                    if data[index] not in b"0123456789abcdefABCDEF":
                        self.invalid = True
                        break
                    self._unicode -= 1
                    stop = index + 1
                elif self._escape:
                    self._escape = False
                    if data[index] == ord("u"):
                        self._unicode = 4
                    elif data[index] not in b'"\\/bfnrt':
                        self.invalid = True
                        break
                    stop = index + 1
                else:
                    match = _STRING_SPECIAL.search(data, index)
                    stop = match.start() + 1 if match else len(data)
                    if match:
                        char = data[match.start()]
                        if char == ord('"'):
                            self._string = False
                        elif char == ord("\\"):
                            self._escape = True
                        else:
                            self.invalid = True
                            break
                if not self._long:
                    if len(self._token) + stop - index <= 256:
                        self._token.extend(data[index:stop])
                    else:
                        self._long = True
                        self._token.clear()
                index = stop
                if not self._string:
                    try:
                        value = _LONG_STRING if self._long else json.loads(self._token)
                    except (ValueError, UnicodeDecodeError):
                        self.invalid = True
                        break
                    self._accept("string", value)
                    self._token.clear()
                continue
            if self._scalar:
                match = _SCALAR_END.search(data, index)
                stop = match.start() if match else len(data)
                if len(self._token) + stop - index > 128:
                    self.invalid = True
                    break
                self._token.extend(data[index:stop])
                index = stop
                if match:
                    try:
                        value = json.loads(self._token)
                    except (ValueError, UnicodeDecodeError):
                        self.invalid = True
                        break
                    self._accept("scalar", value)
                    self._token.clear()
                    self._scalar = False
                continue
            char = data[index]
            index += 1
            if char in b" \r\n\t":
                continue
            if self.complete:
                self.invalid = True
                break
            state = self._stack[-1][1] if self._stack else "value"
            if char == ord('"'):
                self._string = True
                self._long = False
                self._token = bytearray(b'"')
            elif char in b"{[":
                if state not in {"value", "value_or_end"} or len(self._stack) >= 64:
                    self.invalid = True
                    break
                kind = "object" if char == ord("{") else "array"
                self._stack.append([kind, "key_or_end" if kind == "object" else "value_or_end", False])
            elif char in b"}]":
                kind = "object" if char == ord("}") else "array"
                empty_state = "key_or_end" if kind == "object" else "value_or_end"
                if not self._stack or self._stack[-1][0] != kind or state not in {empty_state, "comma_or_end"}:
                    self.invalid = True
                    break
                frame = self._stack.pop()
                self._accept(kind, frame[2])
            elif char == ord(":") and state == "colon":
                self._stack[-1][1] = "value"
            elif char == ord(",") and state == "comma_or_end":
                self._stack[-1][1] = "key" if self._stack[-1][0] == "object" else "value"
            elif char in b"-0123456789tfnNI" and state in {"value", "value_or_end"}:
                self._scalar = True
                self._token = bytearray([char])
            else:
                self.invalid = True

    @property
    def valid(self):
        return self.complete and not self.invalid and not self._stack and not self._string and not self._scalar and not self._utf8.getstate()[0]
