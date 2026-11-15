"""The /usage probe must rotate a stale token instead of locking the channel out.

`reauth_required` makes the credential resolver refuse to work, so it also breaks
the inference path. A 401 from /usage therefore has to be tried as a stale token
first: refresh with the rejected credential, retry once, and only surface
`reauth_required` when the refresh itself is rejected.
"""

import asyncio
import time

from httpx import AsyncClient, MockTransport, Response
import pytest

from rotor.models.channel import Channel
from rotor.services import quota_refresher
from rotor.services.chatgpt_oauth import OAuthError
from tests.test_chatgpt_oauth_backend import database, jwt, seed, tokens

USAGE = {
    "plan_type": "pro",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {"used_percent": 12.0, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 30.0, "limit_window_seconds": 604800},
    },
}


class Upstream:
    """Route /usage and /oauth/token, counting calls and recording auth headers."""

    def __init__(self, *, usage_status=200, token_status=200, stale_first=True):
        self.usage_status = usage_status
        self.token_status = token_status
        self.stale_first = stale_first
        self.usage_calls = []
        self.token_calls = 0

    def handler(self, request):
        if request.url.path.endswith("/oauth/token"):
            self.token_calls += 1
            if self.token_status == 200:
                return Response(200, json=tokens(account="account-2", refresh="refresh-2", lifetime=864000))
            return Response(self.token_status, json={"error": {"code": "invalid_grant"}})
        self.usage_calls.append(request.headers.get("authorization"))
        if self.usage_status != 200:
            return Response(self.usage_status, json={"error": {"message": "nope"}})
        if self.stale_first and len(self.usage_calls) == 1:
            # The token seeded on the channel is stale as far as upstream is concerned.
            return Response(401, json={"error": {"message": "token expired"}})
        return Response(200, json=USAGE)


async def _stored(sessions, channel_id):
    async with sessions() as db:
        return await db.get(Channel, channel_id)


def test_usage_401_refreshes_rejected_token_and_retries(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() + 7200)
            stale = channel.oauth_access_token
            upstream = Upstream()
            async with AsyncClient(transport=MockTransport(upstream.handler)) as client:
                usage = await quota_refresher.fetch_usage(channel, client)

            assert usage.limit_reached is False and usage.plan == "pro"
            assert upstream.usage_calls == [f"Bearer {stale}", f"Bearer {jwt('account-2')}"]
            assert upstream.token_calls == 1

            stored = await _stored(sessions, channel.id)
            assert stored.oauth_access_token == jwt("account-2")
            assert stored.oauth_refresh_token == "refresh-2"
            assert stored.oauth_status == "connected" and stored.oauth_error is None
            # The retry ran on the rotated credential, not the rejected one.
            assert channel._quota_oauth_access_token == jwt("account-2")

    asyncio.run(exercise())


def test_usage_401_marks_reauth_required_only_when_refresh_is_rejected(tmp_path, monkeypatch):
    """A rejected refresh is the real "sign in again", and it still propagates."""

    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() + 7200)
            stale = (channel.oauth_access_token, channel.oauth_refresh_token)
            upstream = Upstream(token_status=400)
            async with AsyncClient(transport=MockTransport(upstream.handler)) as client:
                with pytest.raises(OAuthError) as raised:
                    await quota_refresher.fetch_usage(channel, client)

            assert raised.value.auth_status == "reauth_required"
            assert upstream.usage_calls == [f"Bearer {stale[0]}"]
            assert upstream.token_calls == 1

            stored = await _stored(sessions, channel.id)
            assert stored.oauth_status == "reauth_required"
            assert stored.oauth_access_token == stale[0]

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [500, 502, 503])
def test_usage_transient_failure_does_not_touch_the_token(tmp_path, monkeypatch, status):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() + 7200)
            upstream = Upstream(usage_status=status)
            async with AsyncClient(transport=MockTransport(upstream.handler)) as client:
                with pytest.raises(OAuthError) as raised:
                    await quota_refresher.fetch_usage(channel, client)

            assert raised.value.status_code == 503
            assert raised.value.auth_status == "temporary_error"
            assert upstream.token_calls == 0
            assert len(upstream.usage_calls) == 1

            stored = await _stored(sessions, channel.id)
            assert stored.oauth_status == "connected"
            assert stored.oauth_access_token == channel.oauth_access_token

    asyncio.run(exercise())


def test_usage_403_denial_does_not_refresh(tmp_path, monkeypatch):
    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() + 7200)
            upstream = Upstream(usage_status=403)
            async with AsyncClient(transport=MockTransport(upstream.handler)) as client:
                with pytest.raises(OAuthError) as raised:
                    await quota_refresher.fetch_usage(channel, client)

            assert raised.value.status_code == 503
            assert upstream.token_calls == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("lifetime,rotates", [
    (10 * 60, True),      # inside the window: refreshed before it lapses
    (25 * 60, True),      # still inside the widened window
    (4 * 3600, False),    # comfortably valid: reused as is
])
def test_refresh_window_rotates_a_long_lived_token_well_before_it_lapses(tmp_path, monkeypatch, lifetime, rotates):
    """240h tokens spend ~99% of their life unusable under a 300s window."""

    async def exercise():
        async with database(tmp_path, monkeypatch) as sessions:
            channel = await seed(sessions, oauth_expires_at=time.time() + lifetime)
            upstream = Upstream(stale_first=False)
            async with AsyncClient(transport=MockTransport(upstream.handler)) as client:
                usage = await quota_refresher.fetch_usage(channel, client)

            assert upstream.token_calls == (1 if rotates else 0)
            # Whether or not it rotated, one probe ran and it succeeded.
            assert len(upstream.usage_calls) == 1
            assert usage.limit_reached is False
            assert upstream.usage_calls[0] == (
                f"Bearer {jwt('account-2')}" if rotates else f"Bearer {channel.oauth_access_token}"
            )

    asyncio.run(exercise())
