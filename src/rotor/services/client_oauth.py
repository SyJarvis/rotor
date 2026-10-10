"""Public-client authorization code grants for Rotor's inference plane."""

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import select, update

from rotor.config import OAuthClientSettings, oauth_redirect_parts, settings
from rotor.core.exceptions import AuthenticationException
from rotor.core.user_access import accessible_models, validate_token_access
from rotor.models.admin_auth import AdminUser
from rotor.models.client_oauth import OAuthAuthorizationCode, OAuthCredential, OAuthGrant
from rotor.models.token import Token


ACCESS_PREFIX = "rotor-access-"
CODE_TTL = 300
ACCESS_TTL = 3600
GRANT_TTL = 30 * 24 * 3600
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status_code: int = 400):
        self.error = error
        self.description = description
        self.status_code = status_code


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def registered_client(client_id: str) -> OAuthClientSettings:
    client = settings.ROTOR_OAUTH_CLIENTS.get(client_id)
    if client is None:
        raise OAuthError("invalid_client", "Unknown client")
    # Settings normally parses environment JSON; this also supports runtime
    # configuration in embedders without bypassing redirect validation.
    return OAuthClientSettings.model_validate(client)


def validate_redirect(client: OAuthClientSettings, redirect_uri: str):
    try:
        incoming = oauth_redirect_parts(redirect_uri)
    except (ValueError, TypeError):
        raise OAuthError("invalid_request", "Invalid redirect URI") from None
    for registered in client.redirect_uris:
        expected = oauth_redirect_parts(registered)
        if redirect_uri == registered:
            return
        if (expected.scheme == incoming.scheme == "http"
                and incoming.hostname == expected.hostname
                and incoming.hostname in {"127.0.0.1", "::1"}
                and incoming.path == expected.path):
            return
    raise OAuthError("invalid_request", "Redirect URI is not registered")


@dataclass(frozen=True)
class AuthorizationRequest:
    client_id: str
    redirect_uri: str
    scope: str
    state: str
    code_challenge: str
    client: OAuthClientSettings


def authorization_request(values: dict) -> AuthorizationRequest:
    client_id = values.get("client_id", "")
    if not isinstance(client_id, str):
        raise OAuthError("invalid_request", "Invalid client ID")
    client = registered_client(client_id)
    if values.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "Only authorization code is supported")
    redirect_uri = values.get("redirect_uri", "")
    validate_redirect(client, redirect_uri)
    if values.get("scope", "inference") != "inference":
        raise OAuthError("invalid_scope", "Only inference scope is supported")
    state = values.get("state")
    if not isinstance(state, str) or not state or len(state) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in state):
        raise OAuthError("invalid_request", "A non-empty state of at most 512 characters is required")
    challenge = values.get("code_challenge", "")
    if (not isinstance(challenge, str) or not _CHALLENGE.fullmatch(challenge)
            or values.get("code_challenge_method") != "S256"):
        raise OAuthError("invalid_request", "PKCE S256 is required")
    return AuthorizationRequest(client_id, redirect_uri, "inference", state, challenge, client)


async def owner_token(db, owner_id: int, token_id: int | None = None, signature: str | None = None):
    owner = await db.get(AdminUser, owner_id, populate_existing=True)
    if owner is None or not owner.enabled:
        raise HTTPException(403, "Account is disabled")
    if owner.must_change_password or (signature is not None and not secrets.compare_digest(signature, secret_hash(owner.password_hash))):
        raise HTTPException(403, "Account login has changed; authorize again")
    token_id = owner.default_token_id if token_id is None else token_id
    token = await db.get(Token, token_id, populate_existing=True) if token_id is not None else None
    if token is None or token.owner_id != owner.id:
        raise HTTPException(403, "Authorized API Key is unavailable")
    await validate_token_access(token, db)
    return owner, token


async def authorization_details(db, actor, request: AuthorizationRequest):
    owner, token = await owner_token(db, actor.user_id)
    return {"client_id": request.client_id, "client_name": request.client.name,
            "redirect_uri": request.redirect_uri, "scope": request.scope,
            "username": owner.username, "models": await accessible_models(token, db),
            "quota": owner.quota, "used_quota": owner.used_quota}


async def authorize(db, actor, request: AuthorizationRequest, approve: bool):
    if not approve:
        return request.redirect_uri + "?" + urlencode({"error": "access_denied", "state": request.state})
    owner, token = await owner_token(db, actor.user_id)
    now = utc_now()
    code = "rotor-code-" + secrets.token_urlsafe(32)
    db.add(OAuthAuthorizationCode(secret_hash=secret_hash(code), owner_id=owner.id, token_id=token.id,
        client_id=request.client_id, redirect_uri=request.redirect_uri, code_challenge=request.code_challenge,
        account_auth_signature=secret_hash(owner.password_hash), created_at=now, expires_at=now + timedelta(seconds=CODE_TTL)))
    await db.commit()
    return request.redirect_uri + "?" + urlencode({"code": code, "state": request.state})


def invalid_grant():
    return OAuthError("invalid_grant", "Authorization credential is invalid or expired")


async def validate_grant_owner(db, grant):
    try:
        return await owner_token(db, grant.owner_id, grant.token_id, grant.account_auth_signature)
    except HTTPException as error:
        await db.rollback()
        if error.status_code == 429:
            raise OAuthError("temporarily_unavailable", error.detail, 429) from None
        raise invalid_grant() from None


async def issue_credential(db, grant, now):
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = "rotor-refresh-" + secrets.token_urlsafe(32)
    expires = min(now + timedelta(seconds=ACCESS_TTL), as_utc(grant.expires_at))
    db.add(OAuthCredential(grant_id=grant.id, access_hash=secret_hash(access), refresh_hash=secret_hash(refresh),
        created_at=now, access_expires_at=expires))
    await db.commit()
    return {"access_token": access, "token_type": "Bearer", "expires_in": max(0, int((expires - now).total_seconds())),
            "refresh_token": refresh, "scope": grant.scope}


async def exchange_code(db, values):
    client_id = values.get("client_id", "")
    registered_client(client_id)
    code = values.get("code", "")
    verifier = values.get("code_verifier", "")
    redirect_uri = values.get("redirect_uri", "")
    if not _VERIFIER.fullmatch(verifier) or not code or not redirect_uri:
        raise invalid_grant()
    now = utc_now()
    code_row = (await db.execute(update(OAuthAuthorizationCode).where(
        OAuthAuthorizationCode.secret_hash == secret_hash(code), OAuthAuthorizationCode.client_id == client_id,
        OAuthAuthorizationCode.redirect_uri == redirect_uri, OAuthAuthorizationCode.code_challenge == pkce_challenge(verifier),
        OAuthAuthorizationCode.used_at.is_(None), OAuthAuthorizationCode.expires_at > now,
    ).values(used_at=now).returning(OAuthAuthorizationCode))).scalar_one_or_none()
    if code_row is None:
        raise invalid_grant()
    await validate_grant_owner(db, code_row)
    grant = OAuthGrant(owner_id=code_row.owner_id, token_id=code_row.token_id, client_id=client_id,
        scope="inference", account_auth_signature=code_row.account_auth_signature, created_at=now,
        expires_at=now + timedelta(seconds=GRANT_TTL))
    db.add(grant)
    await db.flush()
    return await issue_credential(db, grant, now)


async def refresh_credential(db, values):
    client_id = values.get("client_id", "")
    registered_client(client_id)
    refresh = values.get("refresh_token", "")
    if not refresh:
        raise invalid_grant()
    now = utc_now()
    live_grants = select(OAuthGrant.id).where(OAuthGrant.client_id == client_id,
        OAuthGrant.revoked_at.is_(None), OAuthGrant.expires_at > now)
    credential = (await db.execute(update(OAuthCredential).where(
        OAuthCredential.refresh_hash == secret_hash(refresh), OAuthCredential.rotated_at.is_(None),
        OAuthCredential.grant_id.in_(live_grants),
    ).values(rotated_at=now).returning(OAuthCredential))).scalar_one_or_none()
    if credential is None:
        replay_grant = await db.scalar(select(OAuthGrant).join(OAuthCredential).where(
            OAuthCredential.refresh_hash == secret_hash(refresh), OAuthCredential.rotated_at.is_not(None),
            OAuthGrant.client_id == client_id, OAuthGrant.revoked_at.is_(None)))
        if replay_grant is not None:
            replay_grant.revoked_at = now
            await db.commit()
        raise invalid_grant()
    grant = await db.get(OAuthGrant, credential.grant_id)
    await validate_grant_owner(db, grant)
    return await issue_credential(db, grant, now)


async def revoke_credential(db, client_id, token):
    registered_client(client_id)
    digest = secret_hash(token)
    grant_ids = select(OAuthCredential.grant_id).where(
        (OAuthCredential.access_hash == digest) | (OAuthCredential.refresh_hash == digest))
    await db.execute(update(OAuthGrant).where(OAuthGrant.id.in_(grant_ids),
        OAuthGrant.client_id == client_id, OAuthGrant.revoked_at.is_(None)).values(revoked_at=utc_now()))
    await db.commit()


async def resolve_access_token(db, access: str):
    now = utc_now()
    grant = await db.scalar(select(OAuthGrant).join(OAuthCredential).where(
        OAuthCredential.access_hash == secret_hash(access), OAuthCredential.access_expires_at > now,
        OAuthGrant.expires_at > now, OAuthGrant.revoked_at.is_(None)))
    if grant is None or grant.client_id not in settings.ROTOR_OAUTH_CLIENTS:
        error = AuthenticationException("Invalid or expired OAuth access token")
        error.headers = {"WWW-Authenticate": "Bearer"}
        raise error
    try:
        _, token = await owner_token(db, grant.owner_id, grant.token_id, grant.account_auth_signature)
    except HTTPException as denied:
        if denied.status_code == 429:
            raise
        error = AuthenticationException("OAuth authorization is no longer valid")
        error.headers = {"WWW-Authenticate": "Bearer"}
        raise error from None
    await db.execute(update(OAuthGrant).where(OAuthGrant.id == grant.id).values(last_used_at=now))
    # Release SQLite's write lock before the gateway awaits an upstream model.
    await db.commit()
    return token
