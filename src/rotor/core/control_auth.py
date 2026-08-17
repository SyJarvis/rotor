from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import secrets
import uuid
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.config import settings
from rotor.database import get_db
from rotor.models.mcp_control_key import MCPControlKey


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
control_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class ActorContext:
    actor_id: str
    client_id: str
    scopes: frozenset[str]
    agent_id: str | None = None
    agent_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class ControlAuthConfig:
    token: str | None
    scopes: frozenset[str]
    actor_id: str
    client_id: str


class ControlAPIException(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def get_control_auth_config() -> ControlAuthConfig:
    return ControlAuthConfig(
        token=settings.ROTOR_CONTROL_API_TOKEN,
        scopes=frozenset(settings.ROTOR_CONTROL_API_SCOPES),
        actor_id=settings.ROTOR_CONTROL_ACTOR_ID,
        client_id=settings.ROTOR_CONTROL_CLIENT_ID,
    )


async def get_control_actor(
    request: Request,
    authorization: HTTPAuthorizationCredentials | None = Depends(
        control_bearer
    ),
    config: ControlAuthConfig = Depends(
        get_control_auth_config
    ),
    db: AsyncSession = Depends(get_db),
) -> ActorContext:
    if authorization is None:
        raise ControlAPIException(
            status_code=401,
            code="control_authentication_required",
            message="Control API authentication is required",
        )

    credential = authorization.credentials
    if credential.startswith(settings.API_KEY_PREFIX):
        raise ControlAPIException(
            status_code=401,
            code="control_invalid_credential",
            message="Invalid Control API credential",
        )

    if config.token is not None and secrets.compare_digest(
        credential, config.token
    ):
        return ActorContext(
            actor_id=config.actor_id,
            client_id=config.client_id,
            scopes=config.scopes,
            agent_id=request.headers.get("X-Agent-Id"),
            agent_run_id=request.headers.get("X-Agent-Run-Id"),
        )

    if credential.startswith("rck_"):
        result = await db.execute(
            select(MCPControlKey).where(
                MCPControlKey.secret_hash
                == hashlib.sha256(credential.encode("utf-8")).hexdigest(),
                MCPControlKey.enabled.is_(True),
            )
        )
        key = result.scalar_one_or_none()
        if key is not None:
            key.last_used_at = datetime.now(timezone.utc)
            return ActorContext(
                actor_id=f"mcp-control-key-{key.id}",
                client_id=f"mcp-control-key-{key.id}",
                scopes=frozenset(key.scopes),
                agent_id=request.headers.get("X-Agent-Id"),
                agent_run_id=request.headers.get("X-Agent-Run-Id"),
            )

    if config.token is None and not credential.startswith("rck_"):
        raise ControlAPIException(
            status_code=503,
            code="control_api_not_configured",
            message="Control API credential is not configured",
        )

    raise ControlAPIException(
        status_code=401,
        code="control_invalid_credential",
        message="Invalid Control API credential",
    )


def require_control_scope(scope: str):
    async def dependency(
        actor: ActorContext = Depends(get_control_actor),
    ) -> ActorContext:
        if scope not in actor.scopes:
            raise ControlAPIException(
                status_code=403,
                code="control_scope_required",
                message=f"Control API scope '{scope}' is required",
                details={"required_scope": scope},
            )
        return actor

    return dependency


def control_request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-Id")
    if supplied and _REQUEST_ID_PATTERN.fullmatch(supplied):
        return supplied
    return f"req_{uuid.uuid4().hex}"


async def control_api_exception_handler(
    request: Request,
    exc: ControlAPIException,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        headers=(
            {"WWW-Authenticate": "Bearer"}
            if exc.status_code == 401
            else None
        ),
        content={
            "schema_version": "1",
            "request_id": control_request_id(request),
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": False,
                "details": exc.details,
            },
        },
    )
