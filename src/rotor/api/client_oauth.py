"""Rotor client login endpoints, isolated from upstream provider OAuth."""

import json
from pathlib import Path
import re
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.config import settings
from rotor.core.admin_auth import AdminActor, require_user_access
from rotor.database import get_db
from rotor.models.client_oauth import OAuthGrant
from rotor.services.client_oauth import (
    OAuthError, as_utc, authorization_details, authorization_request, authorize,
    exchange_code, refresh_credential, revoke_credential, utc_now,
)


_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}
_MAX_BODY = 8192
_AUTH_FIELDS = {"response_type", "client_id", "redirect_uri", "scope", "state", "code_challenge", "code_challenge_method"}


async def oauth_error_handler(request: Request, error: OAuthError):
    return JSONResponse({"error": error.error, "error_description": error.description},
                        status_code=error.status_code, headers=_NO_STORE)


class OAuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store_handler(request):
            try:
                response = await handler(request)
            except OAuthError as error:
                response = await oauth_error_handler(request, error)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
            response.headers.update(_NO_STORE)
            return response
        return no_store_handler


router = APIRouter(tags=["client-oauth"], route_class=OAuthRoute)


def strict_pairs(raw: bytes):
    try:
        text = raw.decode("ascii")
        if re.search(r"%(?![0-9A-Fa-f]{2})", text):
            raise ValueError()
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=20, encoding="utf-8", errors="strict")
        values = {}
        for name, value in pairs:
            if name in values:
                raise ValueError()
            values[name] = value
        return values
    except (UnicodeError, ValueError):
        raise OAuthError("invalid_request", "Malformed or repeated parameters") from None


def query_values(request: Request, *, allow_register: bool = False):
    raw = request.scope.get("query_string", b"")
    if len(raw) > _MAX_BODY:
        raise OAuthError("invalid_request", "Authorization request is too large")
    values = strict_pairs(raw)
    if allow_register and "register" in values:
        if values.pop("register") != "1":
            raise OAuthError("invalid_request", "Invalid registration option")
    if values.keys() - _AUTH_FIELDS:
        raise OAuthError("invalid_request", "Unsupported authorization parameter")
    return values


async def bounded_body(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY:
            raise OAuthError("invalid_request", "Request is too large")
    return bytes(body)


async def form_values(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/x-www-form-urlencoded":
        raise OAuthError("invalid_request", "Use application/x-www-form-urlencoded")
    return strict_pairs(await bounded_body(request))


@router.get("/oauth/authorize", include_in_schema=False)
async def authorization_page(request: Request):
    authorization_request(query_values(request, allow_register=True))
    return FileResponse(_FRONTEND_DIR / "oauth.html", headers={**_NO_STORE,
        "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'"})


@router.get("/oauth/apps", include_in_schema=False)
async def authorized_apps_page():
    return FileResponse(_FRONTEND_DIR / "oauth.html", headers={**_NO_STORE,
        "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'"})


@router.get("/api/oauth/authorization")
async def get_authorization(request: Request, actor: AdminActor = Depends(require_user_access),
                            db: AsyncSession = Depends(get_db)):
    return JSONResponse(await authorization_details(db, actor, authorization_request(query_values(request))), headers=_NO_STORE)


@router.post("/api/oauth/authorization")
async def confirm_authorization(request: Request, actor: AdminActor = Depends(require_user_access),
                                db: AsyncSession = Depends(get_db)):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise OAuthError("invalid_request", "Use application/json")
    try:
        def unique_object(pairs):
            values = {}
            for key, value in pairs:
                if key in values:
                    raise ValueError()
                values[key] = value
            return values
        values = json.loads(await bounded_body(request), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise OAuthError("invalid_request", "Malformed authorization body") from None
    if (not isinstance(values, dict) or values.keys() - (_AUTH_FIELDS | {"approve"})
            or type(values.get("approve")) is not bool
            or any(not isinstance(value, str) for key, value in values.items() if key != "approve")):
        raise OAuthError("invalid_request", "Invalid authorization fields")
    redirect = await authorize(db, actor, authorization_request(values), values["approve"])
    return JSONResponse({"redirect_url": redirect}, headers=_NO_STORE)


@router.post("/oauth/token")
async def token_endpoint(request: Request, db: AsyncSession = Depends(get_db)):
    values = await form_values(request)
    if request.headers.get("authorization") or "client_secret" in values:
        raise OAuthError("invalid_client", "Public clients must not use client authentication")
    grant_type = values.get("grant_type")
    if grant_type == "authorization_code":
        response = await exchange_code(db, values)
    elif grant_type == "refresh_token":
        response = await refresh_credential(db, values)
    else:
        raise OAuthError("unsupported_grant_type", "Unsupported grant type")
    return JSONResponse(response, headers=_NO_STORE)


@router.post("/oauth/revoke")
async def revoke_endpoint(request: Request, db: AsyncSession = Depends(get_db)):
    values = await form_values(request)
    if request.headers.get("authorization") or "client_secret" in values:
        raise OAuthError("invalid_client", "Public clients must not use client authentication")
    if not values.get("token"):
        raise OAuthError("invalid_request", "A token is required")
    await revoke_credential(db, values.get("client_id", ""), values["token"])
    return Response(status_code=200, headers=_NO_STORE)


@router.get("/api/oauth/grants")
async def list_grants(actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    grants = (await db.scalars(select(OAuthGrant).where(OAuthGrant.owner_id == actor.user_id,
        OAuthGrant.revoked_at.is_(None), OAuthGrant.expires_at > utc_now()).order_by(OAuthGrant.created_at.desc(), OAuthGrant.id.desc()))).all()
    return JSONResponse({"items": [{"id": grant.id, "client_id": grant.client_id,
        "client_name": settings.ROTOR_OAUTH_CLIENTS[grant.client_id].name if grant.client_id in settings.ROTOR_OAUTH_CLIENTS else grant.client_id,
        "created_at": as_utc(grant.created_at).isoformat(), "last_used_at": as_utc(grant.last_used_at).isoformat() if grant.last_used_at else None,
        "expires_at": as_utc(grant.expires_at).isoformat()} for grant in grants]}, headers=_NO_STORE)


@router.delete("/api/oauth/grants/{grant_id}", status_code=204)
async def revoke_grant(grant_id: int, actor: AdminActor = Depends(require_user_access), db: AsyncSession = Depends(get_db)):
    result = await db.execute(update(OAuthGrant).where(OAuthGrant.id == grant_id, OAuthGrant.owner_id == actor.user_id,
        OAuthGrant.revoked_at.is_(None)).values(revoked_at=utc_now()))
    if result.rowcount != 1:
        raise HTTPException(404, "Authorization not found")
    await db.commit()
    return Response(status_code=204, headers=_NO_STORE)


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    issuer = settings.ROTOR_OAUTH_ISSUER or str(request.base_url).rstrip("/")
    return JSONResponse({"issuer": issuer, "authorization_endpoint": issuer + "/oauth/authorize",
        "token_endpoint": issuer + "/oauth/token", "revocation_endpoint": issuer + "/oauth/revoke",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"], "scopes_supported": ["inference"]}, headers=_NO_STORE)
