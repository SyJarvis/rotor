"""ChatGPT OAuth transport for the Codex Responses backend."""

from __future__ import annotations

from typing import Any, AsyncIterator

from httpx import HTTPStatusError, Response

from rotor.adapters.protocol.responses import OpenAIResponsesAdapter, _content_text, extract_responses_usage
from rotor.core.exceptions import UpstreamProtocolError
from rotor.schemas.request import ChatCompletionRequest


class OpenAICodexAdapter(OpenAIResponsesAdapter):
    """Reuse Responses validation and conversion with Codex's SSE transport."""

    _access_token = ""
    _account_id = ""

    def responses_url(self, suffix: str = "") -> str:
        return f"https://chatgpt.com/backend-api/codex/responses{suffix}"

    async def get_request_url(self, request: ChatCompletionRequest) -> str:
        return self.responses_url()

    def build_request_headers(self) -> dict[str, str]:
        if not self._access_token or not self._account_id:
            raise UpstreamProtocolError("ChatGPT OAuth credentials are unavailable", status_code=401)
        reserved = {
            "authorization", "proxy-authorization", "cookie", "x-api-key", "api-key",
            "chatgpt-account-id", "originator", "openai-beta", "accept", "content-type",
        }
        headers = {
            name: value for name, value in ((self.channel.extra or {}).get("headers") or {}).items()
            if name.lower() not in reserved
        }
        headers.update({
            "Authorization": f"Bearer {self._access_token}",
            "chatgpt-account-id": self._account_id,
            "originator": "rotor",
            "OpenAI-Beta": "responses=experimental",
            "accept": "text/event-stream",
            "Content-Type": "application/json",
        })
        return headers

    async def make_request(
        self,
        request: ChatCompletionRequest,
        timeout: float | None = None,
    ) -> Response:
        from rotor.services.chatgpt_oauth import resolve_oauth_credentials

        credentials = await resolve_oauth_credentials(self.channel, self.http_client)
        self._access_token = credentials.access_token
        self._account_id = credentials.account_id
        # Only the provider transport streams; the original client preference
        # still controls gateway conversion and delivery.
        upstream_request = request.model_copy(update={"stream": True})
        try:
            return await super().make_request(upstream_request, timeout)
        except HTTPStatusError as exc:
            safe_headers = {name: self._redact(value) for name, value in exc.response.headers.items()}
            try:
                body = self._sanitize_error(exc.response.json())
                sanitized = Response(exc.response.status_code, headers=safe_headers, json=body, request=exc.request)
            except ValueError:
                sanitized = Response(
                    exc.response.status_code, headers=safe_headers,
                    text=self._redact(exc.response.text), request=exc.request,
                )
            raise HTTPStatusError(
                f"ChatGPT Codex returned HTTP {exc.response.status_code}",
                request=exc.request, response=sanitized,
            ) from None

    async def convert_request(self, request: ChatCompletionRequest) -> dict[str, Any]:
        body = await super().convert_request(request)
        if isinstance(body.get("input"), str):
            body["input"] = [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": body["input"]}],
            }]
        if body.get("store") is True or any(
            body.get(name) for name in ("background", "previous_response_id", "conversation")
        ):
            raise UpstreamProtocolError(
                "ChatGPT OAuth does not support stored or background responses", status_code=400,
            )
        body["store"] = False
        body["stream"] = True
        if request.responses_payload is None:
            for name in ("temperature", "top_p"):
                if name not in request.model_fields_set:
                    body.pop(name, None)
            system_items = [
                item for item in body.get("input", [])
                if isinstance(item, dict) and item.get("role") in {"system", "developer"}
            ]
            body["instructions"] = "\n\n".join(_content_text(item.get("content")) for item in system_items)
            body["input"] = [item for item in body.get("input", []) if item not in system_items]
        instructions = body.get("instructions")
        if isinstance(instructions, list):
            instructions = _content_text(instructions)
        body["instructions"] = instructions if isinstance(instructions, str) and instructions.strip() else "You are a helpful assistant."
        return body

    async def convert_response(
        self,
        response: Response,
        request: ChatCompletionRequest,
    ) -> dict[str, Any]:
        terminal = None
        try:
            async for event in self._validated_stream_events(response):
                if event["type"] in {"response.completed", "response.incomplete"}:
                    terminal = event["response"]
            buffered = Response(response.status_code, json=terminal, request=response.request)
            return await super().convert_response(buffered, request)
        except UpstreamProtocolError as exc:
            if terminal is not None:
                usage = extract_responses_usage(terminal)
                if usage is not None:
                    exc.provider_usage = usage
            raise
        finally:
            await response.aclose()

    async def request_resource(self, *args, **kwargs) -> Response:
        raise UpstreamProtocolError("ChatGPT OAuth does not support Responses resources", status_code=400)

    async def _validated_stream_events(self, response: Response) -> AsyncIterator[dict[str, Any]]:
        async for event in super()._validated_stream_events(response):
            if event["type"] in {"response.completed", "response.incomplete"}:
                self._terminal_usage = extract_responses_usage(event["response"])
            yield event

    async def stream_convert_response(
        self, response: Response, request: ChatCompletionRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        self._terminal_usage = None
        try:
            async for event in super().stream_convert_response(response, request):
                yield event
        except UpstreamProtocolError as exc:
            if self._terminal_usage is not None:
                exc.provider_usage = self._terminal_usage
            exc.upstream_status = response.status_code
            raise

    def _redact(self, message: str) -> str:
        for secret in (
            self._access_token, self._account_id, getattr(self.channel, "oauth_refresh_token", None),
            getattr(self.channel, "oauth_access_token", None), getattr(self.channel, "oauth_account_id", None),
        ):
            if secret:
                message = message.replace(secret, "[redacted]")
        return message

    def _sanitize_error(self, value: Any) -> Any:
        if isinstance(value, dict):
            sensitive = {
                "access_token", "refresh_token", "id_token", "authorization", "token", "account_id",
                "chatgpt_account_id", "chatgpt-account-id", "api-key", "api_key", "apikey", "key",
                "cookie", "set-cookie", "x-api-key", "password", "client_secret",
            }
            return {
                name: "[redacted]" if str(name).lower() in sensitive else self._sanitize_error(item)
                for name, item in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize_error(item) for item in value]
        return self._redact(value) if isinstance(value, str) else value

    def _stream_error_details(self, event: dict[str, Any], event_type: str) -> tuple[str, str | None]:
        message, error_type = super()._stream_error_details(self._sanitize_error(event), event_type)
        return self._redact(message), self._redact(error_type) if error_type else None
