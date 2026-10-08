"""ChatGPT browser authorization and persisted credential rotation."""

import asyncio
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.database import async_session_maker
from rotor.models.channel import Channel
from rotor.models.channel_oauth import ChannelOAuthSession
from rotor.schemas.channel_oauth import OAuthCallbackResponse, OAuthSessionResponse


CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
TOKEN_URL = f"{AUTH_BASE_URL}/oauth/token"
AUTHORIZE_URL = f"{AUTH_BASE_URL}/oauth/authorize"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
# The model catalog uses Codex protocol compatibility, independently of Rotor's version.
CATALOG_CLIENT_VERSION = "0.161.0"
HTTP_TIMEOUT = 15.0
LOGIN_LIFETIME = 900.0
REFRESH_WINDOW = 300.0
LOCK_LIFETIME = 60.0
_SESSION_SECRETS = dict(
    device_auth_id=None, user_code=None, access_token=None, refresh_token=None,
    token_expires_at=None, account_id=None, poll_owner=None, poll_lock_until=None,
    oauth_state=None, pkce_verifier=None,
)
_CHANNEL_SECRETS = dict(
    oauth_access_token=None, oauth_refresh_token=None, oauth_expires_at=None,
    oauth_account_id=None, oauth_status=None, oauth_error=None,
    oauth_refresh_owner=None, oauth_refresh_lock_until=None,
)


class OAuthError(httpx.HTTPStatusError):
    def __init__(self, message: str, status_code: int = 401):
        request = httpx.Request("POST", TOKEN_URL)
        response = httpx.Response(status_code, request=request, json={"error": {"message": message}})
        super().__init__(message, request=request, response=response)
        self.status_code = status_code


@dataclass(frozen=True, repr=False)
class OAuthCredentials:
    access_token: str
    account_id: str
    expires_at: float


@dataclass(frozen=True, repr=False)
class _TokenCredentials(OAuthCredentials):
    refresh_token: str


@dataclass
class _Lease:
    owner: str
    claimed: bool = False


def _string(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OAuthError(f"ChatGPT authorization returned an invalid {field}; sign in again")
    return value


def _positive_number(value, field: str, *, allow_string: bool = False, allow_zero: bool = False) -> float:
    numeric = None
    if (isinstance(value, (int, float)) and not isinstance(value, bool)) or (allow_string and isinstance(value, str)):
        try:
            numeric = float(value)
        except (ValueError, OverflowError):
            pass
    if numeric is None or not math.isfinite(numeric) or numeric < 0 or (numeric == 0 and not allow_zero):
        raise OAuthError(f"ChatGPT authorization returned an invalid {field}; sign in again")
    return numeric


def extract_account_id(access_token: str) -> str:
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            raise ValueError
        raw = base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        auth = payload.get("https://api.openai.com/auth")
        account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        return _string(account_id, "account identifier")
    except (ValueError, TypeError, AttributeError, binascii.Error, UnicodeDecodeError):
        raise OAuthError("ChatGPT authorization returned an invalid account identifier; sign in again") from None


def _credentials(payload) -> _TokenCredentials:
    if not isinstance(payload, dict):
        raise OAuthError("ChatGPT authorization returned invalid credentials; sign in again")
    access = _string(payload.get("access_token"), "access token")
    refresh = _string(payload.get("refresh_token"), "refresh token")
    lifetime = _positive_number(payload.get("expires_in"), "token lifetime")
    expires_at = time.time() + lifetime
    if not math.isfinite(expires_at):
        raise OAuthError("ChatGPT authorization returned an invalid token lifetime; sign in again")
    return _TokenCredentials(access, extract_account_id(access), expires_at, refresh)


async def _post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    try:
        async with asyncio.timeout(HTTP_TIMEOUT):
            return await client.post(url, timeout=HTTP_TIMEOUT, follow_redirects=False, **kwargs)
    except (httpx.HTTPError, TimeoutError):
        raise OAuthError("ChatGPT authorization is temporarily unavailable; try again", 503) from None


def _json(response: httpx.Response):
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError):
        raise OAuthError("ChatGPT authorization returned an invalid response; sign in again") from None


async def _token(client: httpx.AsyncClient, data: dict) -> _TokenCredentials:
    response = await _post(client, TOKEN_URL, data={"client_id": CLIENT_ID, **data})
    if not response.is_success:
        if response.status_code >= 500 or response.status_code == 429:
            raise OAuthError("ChatGPT authorization is temporarily unavailable; try again", 503)
        raise OAuthError("ChatGPT authorization failed; sign in again")
    return _credentials(_json(response))


async def fetch_model_catalog(client: httpx.AsyncClient, credential: OAuthCredentials, *, refresh_tokens: tuple[str | None, ...] = ()) -> tuple[list[str], int]:
    try:
        async with asyncio.timeout(HTTP_TIMEOUT):
            response = await client.get(
                CODEX_BASE_URL + "/models", params={"client_version": CATALOG_CLIENT_VERSION},
                headers={"Authorization": "Bearer " + credential.access_token,
                         "chatgpt-account-id": credential.account_id, "originator": "rotor", "Accept": "application/json"},
                timeout=HTTP_TIMEOUT, follow_redirects=False,
            )
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise OAuthError("ChatGPT model catalog is temporarily unavailable; try again", 503) from None
    if not response.is_success:
        if response.status_code in (401, 403):
            raise OAuthError("ChatGPT model catalog authorization failed; sign in again")
        raise OAuthError("ChatGPT model catalog is temporarily unavailable; try again", 503)
    try:
        payload = response.json()
        if not isinstance(payload, dict) or "error" in payload or not isinstance(payload.get("models"), list):
            raise ValueError
        entries = payload["models"]
        models = []
        protected = (credential.access_token, credential.account_id, *refresh_tokens)
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError
            slug = entry.get("slug")
            if (not isinstance(slug, str) or not slug or len(slug) > 255
                    or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in slug)
                    or any(value and value in slug for value in protected)
                    or entry.get("visibility") not in ("list", "hide", "none")
                    or not isinstance(entry.get("supported_in_api"), bool)):
                raise ValueError
            if slug not in models:
                models.append(slug)
        return models, len(entries)
    except (ValueError, TypeError, UnicodeDecodeError):
        raise OAuthError("ChatGPT returned an invalid model catalog; try again", 502) from None


async def probe_session_models(db: AsyncSession, client: httpx.AsyncClient, session_id: str, admin_user_id: int) -> tuple[list[str], int]:
    session = await db.scalar(select(ChannelOAuthSession).where(
        ChannelOAuthSession.id == session_id, ChannelOAuthSession.admin_user_id == admin_user_id,
    ).execution_options(populate_existing=True))
    if session is None:
        raise OAuthError("ChatGPT login session not found", 404)
    if session.status != "authorized":
        raise OAuthError("Complete ChatGPT login before probing models", 409)
    if _positive_number(session.expires_at, "session expiry") <= time.time():
        raise OAuthError("ChatGPT login expired; start again", 400)
    credential = OAuthCredentials(
        _string(session.access_token, "access token"), _string(session.account_id, "account identifier"),
        _positive_number(session.token_expires_at, "token expiry"),
    )
    if credential.expires_at <= time.time():
        raise OAuthError("ChatGPT authorization expired; sign in again")
    return await fetch_model_catalog(client, credential, refresh_tokens=(session.refresh_token,))


def session_response(session: ChannelOAuthSession) -> OAuthSessionResponse:
    authorization_url = None
    if session.status == "pending" and session.oauth_state and session.pkce_verifier:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(session.pkce_verifier.encode("ascii")).digest()).decode().rstrip("=")
        authorization_url = AUTHORIZE_URL + "?" + urlencode({
            "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
            "scope": SCOPE, "state": session.oauth_state, "code_challenge": challenge,
            "code_challenge_method": "S256", "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true", "originator": "rotor",
        })
    return OAuthSessionResponse(
        session_id=session.id, status=session.status,
        authorization_url=authorization_url, redirect_uri=REDIRECT_URI,
        expires_at=session.expires_at, error=session.error,
    )


async def expire_sessions(db: AsyncSession) -> None:
    await db.execute(update(ChannelOAuthSession).where(
        ChannelOAuthSession.expires_at <= time.time(),
        ChannelOAuthSession.status.in_(["pending", "exchanging", "authorized"]),
    ).values(status="expired", error="ChatGPT login expired; start again", **_SESSION_SECRETS))
    await db.commit()


async def start_session(db: AsyncSession, *, admin_user_id: int, channel_id: int | None) -> OAuthSessionResponse:
    await expire_sessions(db)
    if channel_id is not None:
        channel = await db.get(Channel, channel_id)
        if channel is None:
            raise OAuthError("Channel not found", 404)
    now = time.time()
    session = ChannelOAuthSession(
        id=secrets.token_urlsafe(32), admin_user_id=admin_user_id, channel_id=channel_id,
        status="pending", oauth_state=secrets.token_hex(16), pkce_verifier=secrets.token_urlsafe(32),
        # Required legacy device columns are unused by browser authorization.
        interval=0, expires_at=now + LOGIN_LIFETIME, next_poll_at=0,
    )
    db.add(session)
    await db.commit()
    return session_response(session)


async def _session(db: AsyncSession, session_id: str, admin_user_id: int) -> ChannelOAuthSession:
    await expire_sessions(db)
    session = await db.scalar(select(ChannelOAuthSession).where(
        ChannelOAuthSession.id == session_id,
        ChannelOAuthSession.admin_user_id == admin_user_id,
    ).execution_options(populate_existing=True))
    if session is None:
        raise OAuthError("ChatGPT login session not found", 404)
    return session


async def cancel_session(db: AsyncSession, session_id: str, admin_user_id: int) -> OAuthSessionResponse:
    session = await _session(db, session_id, admin_user_id)
    await db.execute(update(ChannelOAuthSession).where(
        ChannelOAuthSession.id == session_id, ChannelOAuthSession.status.in_(["pending", "exchanging", "authorized"]),
    ).values(status="cancelled", error=None, **_SESSION_SECRETS))
    await db.commit()
    await db.refresh(session)
    return session_response(session)


def _validate_callback(session: ChannelOAuthSession, callback_url: str) -> OAuthCallbackResponse:
    if session.status != "pending" or not session.oauth_state or not session.pkce_verifier:
        raise OAuthError("ChatGPT login is no longer awaiting authorization; start again", 409)
    message = "Paste the complete callback URL from this ChatGPT login"
    try:
        value = callback_url.strip()
        value.encode("utf-8")
        if any(ord(character) < 32 or ord(character) == 127 for character in value) or "#" in value:
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme != "http" or parsed.netloc != "localhost:1455" or parsed.path != "/auth/callback":
            raise ValueError
        if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query):
            raise ValueError
        parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=20)
        names = [name for name, _value in parameters]
        if len(names) != len(set(names)) or "error" in names:
            raise ValueError
        values = dict(parameters)
        code, state = values.get("code"), values.get("state")
        if not code or not code.strip() or not state or not state.strip():
            raise ValueError
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise OAuthError(message, 400) from None
    if not secrets.compare_digest(state.encode("utf-8"), session.oauth_state.encode("utf-8")):
        raise OAuthError("The callback does not match this ChatGPT login; use its authorization link", 400)
    return OAuthCallbackResponse(code=code, state=state, redirect_uri=REDIRECT_URI)


async def parse_callback(db: AsyncSession, session_id: str, admin_user_id: int, callback_url: str) -> OAuthCallbackResponse:
    session = await _session(db, session_id, admin_user_id)
    if session.expires_at <= time.time():
        raise OAuthError("ChatGPT login expired; start again", 400)
    return _validate_callback(session, callback_url)


async def complete_session(db: AsyncSession, client: httpx.AsyncClient, session_id: str, admin_user_id: int, callback_url: str) -> OAuthSessionResponse:
    lease = _Lease(secrets.token_hex(16))
    try:
        return await _complete_session(db, client, session_id, admin_user_id, callback_url, lease)
    finally:
        if lease.claimed:
            async def release():
                await db.rollback()
                await db.execute(update(ChannelOAuthSession).where(
                    ChannelOAuthSession.id == session_id, ChannelOAuthSession.poll_owner == lease.owner,
                ).values(status="failed", error="ChatGPT login was interrupted; start again", **_SESSION_SECRETS))
                await db.commit()
            await asyncio.shield(release())


async def _complete_session(db: AsyncSession, client: httpx.AsyncClient, session_id: str, admin_user_id: int, callback_url: str, lease: _Lease) -> OAuthSessionResponse:
    owner = lease.owner
    session = await _session(db, session_id, admin_user_id)
    now = time.time()
    if session.expires_at <= now:
        raise OAuthError("ChatGPT login expired; start again", 400)
    callback = _validate_callback(session, callback_url)
    verifier = session.pkce_verifier
    acquired = await db.execute(update(ChannelOAuthSession).where(
        ChannelOAuthSession.id == session_id, ChannelOAuthSession.status == "pending",
        ChannelOAuthSession.expires_at > now, ChannelOAuthSession.oauth_state == callback.state,
        ChannelOAuthSession.pkce_verifier == verifier, ChannelOAuthSession.poll_owner.is_(None),
    ).values(status="exchanging", poll_owner=owner, poll_lock_until=now + LOCK_LIFETIME,
             oauth_state=None, pkce_verifier=None))
    lease.claimed = bool(acquired.rowcount)
    await db.commit()
    if not acquired.rowcount:
        raise OAuthError("ChatGPT login completion has already started; start again", 409)
    await db.refresh(session)
    if session.status != "exchanging" or session.poll_owner != owner:
        return session_response(session)
    values = {}
    try:
        if time.time() >= session.expires_at:
            raise OAuthError("ChatGPT login expired; start again", 400)
        credential = await _token(client, {
            "grant_type": "authorization_code", "code": callback.code,
            "code_verifier": verifier, "redirect_uri": REDIRECT_URI,
        })
        values = dict(status="authorized", oauth_state=None, pkce_verifier=None,
                      access_token=credential.access_token, refresh_token=credential.refresh_token,
                      token_expires_at=credential.expires_at, account_id=credential.account_id, error=None)
    except OAuthError as exc:
        values = dict(status="failed", error=str(exc), **_SESSION_SECRETS)
    if time.time() >= session.expires_at:
        values = dict(status="expired", error="ChatGPT login expired; start again", **_SESSION_SECRETS)
    values.update(poll_owner=None, poll_lock_until=None)
    await db.execute(update(ChannelOAuthSession).where(
        ChannelOAuthSession.id == session_id, ChannelOAuthSession.poll_owner == owner,
        ChannelOAuthSession.status == "exchanging",
    ).values(**values))
    await db.commit()
    await db.refresh(session)
    return session_response(session)


async def consume_session(db: AsyncSession, session_id: str, *, admin_user_id: int, channel_id: int | None) -> _TokenCredentials:
    now = time.time()
    session = await db.scalar(select(ChannelOAuthSession).where(
        ChannelOAuthSession.id == session_id, ChannelOAuthSession.admin_user_id == admin_user_id,
        ChannelOAuthSession.channel_id == channel_id,
        ChannelOAuthSession.status == "authorized", ChannelOAuthSession.expires_at > now,
        ChannelOAuthSession.token_expires_at > now,
    ))
    if session is None:
        raise OAuthError("ChatGPT login is not ready, expired, or already used; sign in again", 400)
    credential = _TokenCredentials(
        _string(session.access_token, "access token"),
        _string(session.account_id, "account identifier"), session.token_expires_at,
        _string(session.refresh_token, "refresh token"),
    )
    result = await db.execute(update(ChannelOAuthSession).where(
        ChannelOAuthSession.id == session_id, ChannelOAuthSession.status == "authorized",
        ChannelOAuthSession.expires_at > time.time(),
    ).values(status="consumed", error=None, **_SESSION_SECRETS))
    if not result.rowcount:
        raise OAuthError("ChatGPT login has already been used; sign in again", 400)
    # The caller commits this transition together with the channel credentials.
    return credential


def credential_values(credential: _TokenCredentials) -> dict:
    return dict(
        key="", oauth_access_token=credential.access_token, oauth_refresh_token=credential.refresh_token,
        oauth_expires_at=credential.expires_at, oauth_account_id=credential.account_id,
        oauth_status="connected", oauth_error=None, oauth_refresh_owner=None, oauth_refresh_lock_until=None,
    )


def clear_credential_values() -> dict:
    return dict(_CHANNEL_SECRETS)


def validate_configuration(*, auth_type: str, provider_type: str, protocol: str, base_url: str, key: str | None, extra: dict) -> None:
    if auth_type == "oauth":
        if provider_type != "openai_codex" or protocol != "openai_responses" or base_url.rstrip("/") != CODEX_BASE_URL:
            raise OAuthError("ChatGPT OAuth requires the OpenAI Codex provider, Responses protocol, and its default URL", 400)
        if key:
            raise OAuthError("ChatGPT OAuth channels do not accept an API key", 400)
        if any(name in extra for name in ("models_path", "request_path")) and (
            extra.get("models_path") not in (None, "", "/models") or extra.get("request_path") not in (None, "/responses")
        ):
            raise OAuthError("ChatGPT OAuth channels use their default request paths", 400)
        if any(name.lower() in {"authorization", "chatgpt-account-id", "cookie", "host"} for name in (extra.get("headers") or {})):
            raise OAuthError("ChatGPT OAuth authentication headers cannot be overridden", 400)
    elif provider_type == "openai_codex":
        raise OAuthError("The OpenAI Codex provider requires ChatGPT OAuth", 400)
    elif not key:
        raise OAuthError("An API key is required", 400)


def _stored_credential(channel: Channel) -> _TokenCredentials:
    return _TokenCredentials(
        _string(channel.oauth_access_token, "access token"),
        _string(channel.oauth_account_id, "account identifier"),
        _positive_number(channel.oauth_expires_at, "token expiry"),
        _string(channel.oauth_refresh_token, "refresh token"),
    )


async def resolve_oauth_credentials(channel: Channel, http_client: httpx.AsyncClient) -> OAuthCredentials:
    """Rotate under a DB lease, rereading tokens before and after contention."""
    if channel.id is None:
        raise OAuthError("Save and sign in to this ChatGPT channel first")
    lease = _Lease(secrets.token_hex(16))
    try:
        return await _resolve_oauth_credentials(channel, http_client, lease)
    finally:
        if lease.claimed:
            async def release():
                async with async_session_maker() as db:
                    await db.execute(update(Channel).where(
                        Channel.id == channel.id, Channel.oauth_refresh_owner == lease.owner,
                    ).values(oauth_refresh_owner=None, oauth_refresh_lock_until=None))
                    await db.commit()
            await asyncio.shield(release())


async def _resolve_oauth_credentials(channel: Channel, http_client: httpx.AsyncClient, lease: _Lease) -> OAuthCredentials:
    owner = lease.owner
    deadline = time.monotonic() + LOCK_LIFETIME + HTTP_TIMEOUT
    while time.monotonic() < deadline:
        async with async_session_maker() as db:
            current = await db.get(Channel, channel.id)
            if current is None or current.auth_type != "oauth":
                raise OAuthError("ChatGPT authorization is no longer available; sign in again")
            credential = _stored_credential(current)
            if credential.expires_at > time.time() + REFRESH_WINDOW:
                return OAuthCredentials(credential.access_token, credential.account_id, credential.expires_at)
            now = time.time()
            acquired = await db.execute(update(Channel).where(
                Channel.id == channel.id, Channel.auth_type == "oauth",
                Channel.oauth_expires_at <= now + REFRESH_WINDOW,
                or_(Channel.oauth_refresh_lock_until.is_(None), Channel.oauth_refresh_lock_until <= now),
            ).values(oauth_refresh_owner=owner, oauth_refresh_lock_until=now + LOCK_LIFETIME))
            lease.claimed = lease.claimed or bool(acquired.rowcount)
            await db.commit()
            if acquired.rowcount:
                await db.refresh(current)
                if current.oauth_refresh_owner != owner:
                    continue
                credential = _stored_credential(current)
                if credential.expires_at > time.time() + REFRESH_WINDOW:
                    await db.execute(update(Channel).where(
                        Channel.id == channel.id, Channel.oauth_refresh_owner == owner,
                    ).values(oauth_refresh_owner=None, oauth_refresh_lock_until=None))
                    await db.commit()
                    return OAuthCredentials(credential.access_token, credential.account_id, credential.expires_at)
                break
        await asyncio.sleep(0.1)
    else:
        raise OAuthError("ChatGPT authorization is busy; try again", 503)
    try:
        refreshed = await _token(http_client, {"grant_type": "refresh_token", "refresh_token": credential.refresh_token})
        async with async_session_maker() as db:
            saved = await db.execute(update(Channel).where(
                Channel.id == channel.id, Channel.auth_type == "oauth", Channel.oauth_refresh_owner == owner,
            ).values(**credential_values(refreshed)))
            await db.commit()
        if not saved.rowcount:
            # A saved relogin or authentication change invalidated this refresh.
            return await resolve_oauth_credentials(channel, http_client)
        return OAuthCredentials(refreshed.access_token, refreshed.account_id, refreshed.expires_at)
    except OAuthError as exc:
        async with async_session_maker() as db:
            marked = await db.execute(update(Channel).where(
                Channel.id == channel.id, Channel.oauth_refresh_owner == owner,
            ).values(oauth_status="error", oauth_error=str(exc)))
            await db.commit()
        if not marked.rowcount:
            return await resolve_oauth_credentials(channel, http_client)
        raise
