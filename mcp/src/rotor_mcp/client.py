import json
import re
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx

from rotor_mcp.config import Settings
from rotor_mcp.errors import (
    ConfigurationError,
    ControlAPIError,
    InvalidControlAPIResponse,
    InvalidToolInput,
    RotorBackendUnavailable,
)


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class RotorControlClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=settings.control_api_timeout_seconds,
        )

    async def __aenter__(self) -> "RotorControlClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def get_request_trace(
        self,
        request_id: str,
        *,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not _REQUEST_ID_PATTERN.fullmatch(request_id):
            raise InvalidToolInput(
                "request_id must be 1-64 characters using only letters, "
                "numbers, '.', '_', ':' or '-'"
            )

        payload = await self._get(
            f"/requests/{request_id}/trace",
            agent_run_id=agent_run_id,
        )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("request_id") != request_id:
            raise InvalidControlAPIResponse(
                "Rotor Control API returned an invalid request trace"
            )
        return payload

    async def list_channels(
        self,
        *,
        enabled: bool | None = None,
        model: str | None = None,
        protocol: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload = await self._get(
            "/channels",
            params={
                "enabled": enabled,
                "model": model,
                "protocol": protocol,
                "cursor": cursor,
                "limit": limit,
            },
        )
        if not isinstance(payload.get("data"), list) or not isinstance(
            payload.get("page"),
            dict,
        ):
            raise InvalidControlAPIResponse(
                "Rotor Control API returned an invalid channel list"
            )
        return payload

    async def get_channel(self, channel_id: int) -> dict[str, Any]:
        if (
            not isinstance(channel_id, int)
            or isinstance(channel_id, bool)
            or channel_id < 1
        ):
            raise InvalidToolInput(
                "channel_id must be a positive integer"
            )
        payload = await self._get(f"/channels/{channel_id}")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("id") != channel_id:
            raise InvalidControlAPIResponse(
                "Rotor Control API returned an invalid channel"
            )
        return payload

    async def list_recent_failures(
        self,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        model: str | None = None,
        channel_id: int | None = None,
        category: str | None = None,
        group_by: str = "category",
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload = await self._get(
            "/failures",
            params={
                "start_time": (
                    start_time.isoformat() if start_time else None
                ),
                "end_time": end_time.isoformat() if end_time else None,
                "model": model,
                "channel_id": channel_id,
                "category": category,
                "group_by": group_by,
                "cursor": cursor,
                "limit": limit,
            },
        )
        if not isinstance(payload.get("data"), list) or not isinstance(
            payload.get("page"),
            dict,
        ):
            raise InvalidControlAPIResponse(
                "Rotor Control API returned an invalid failures page"
            )
        return payload

    async def list_model_usage(
        self,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload = await self._get(
            "/usage/models",
            params={
                "start_time": (
                    start_time.isoformat() if start_time else None
                ),
                "end_time": end_time.isoformat() if end_time else None,
                "limit": limit,
            },
        )
        if not isinstance(payload.get("data"), list) or not isinstance(
            payload.get("window"),
            dict,
        ):
            raise InvalidControlAPIResponse(
                "Rotor Control API returned an invalid model usage list"
            )
        return payload

    async def evaluate_session_leases(
        self,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        payload = await self._get(
            "/usage/session-leases",
            params={
                "start_time": (
                    start_time.isoformat() if start_time else None
                ),
                "end_time": end_time.isoformat() if end_time else None,
                "model": model,
            },
        )
        if not isinstance(payload.get("data"), dict) or not isinstance(
            payload.get("window"),
            dict,
        ):
            raise InvalidControlAPIResponse(
                "Rotor Control API returned an invalid Session Lease "
                "evaluation"
            )
        return payload

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        token = self._settings.control_api_token
        if token is None or not token.get_secret_value():
            raise ConfigurationError(
                "ROTOR_CONTROL_API_TOKEN is required to call Rotor"
            )

        headers = {
            "Authorization": f"Bearer {token.get_secret_value()}",
            "X-Request-Id": f"mcp_{uuid4().hex}",
            "X-Agent-Id": self._settings.agent_id,
        }
        active_run_id = agent_run_id or self._settings.agent_run_id
        if active_run_id:
            if (
                len(active_run_id) > 128
                or "\r" in active_run_id
                or "\n" in active_run_id
            ):
                raise InvalidToolInput(
                    "agent_run_id must be 1-128 characters and contain "
                    "no line breaks"
                )
            headers["X-Agent-Run-Id"] = active_run_id

        url = f"{self._settings.control_api_url}{path}"
        try:
            async with self._http_client.stream(
                "GET",
                url,
                headers=headers,
                params={
                    key: value
                    for key, value in (params or {}).items()
                    if value is not None
                },
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if (
                        len(content) + len(chunk)
                        > self._settings.control_api_max_response_bytes
                    ):
                        raise InvalidControlAPIResponse(
                            "Rotor Control API response exceeded the "
                            "configured size limit"
                        )
                    content.extend(chunk)
                status_code = response.status_code
        except httpx.TimeoutException as exc:
            raise RotorBackendUnavailable(
                "Rotor Control API request timed out"
            ) from exc
        except httpx.RequestError as exc:
            raise RotorBackendUnavailable(
                "Rotor Control API is unavailable"
            ) from exc

        payload = self._decode_payload(bytes(content))
        if not 200 <= status_code < 300:
            raise self._map_api_error(status_code, payload)
        return payload

    @staticmethod
    def _decode_payload(content: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidControlAPIResponse(
                "Rotor Control API returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise InvalidControlAPIResponse(
                "Rotor Control API returned an invalid response envelope"
            )
        return payload

    @staticmethod
    def _map_api_error(
        status_code: int,
        payload: dict[str, Any],
    ) -> ControlAPIError:
        error = payload.get("error")
        if not isinstance(error, dict):
            return ControlAPIError(
                status_code=status_code,
                code="control_api_error",
                message=f"Rotor Control API returned HTTP {status_code}",
                retryable=status_code in {429, 502, 503, 504},
            )

        code = error.get("code")
        message = error.get("message")
        details = error.get("details")
        return ControlAPIError(
            status_code=status_code,
            code=code if isinstance(code, str) else "control_api_error",
            message=(
                message[:500]
                if isinstance(message, str)
                else f"Rotor Control API returned HTTP {status_code}"
            ),
            retryable=bool(error.get("retryable", False)),
            details=details if isinstance(details, dict) else None,
        )
