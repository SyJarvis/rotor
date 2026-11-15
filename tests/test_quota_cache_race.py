"""Deterministic quota/configuration races and cache identity fences."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from rotor.models.channel import Channel
from rotor.schemas.channel import ChannelUsageResponse
from rotor.services import quota_refresher, usage_cache
from tests.test_zhipu_subscription_state import environment


@pytest.mark.parametrize("path", ["admin", "background"])
@pytest.mark.parametrize("change", ["key", "provider", "delete"])
def test_inflight_result_rejected_after_other_process_change(tmp_path, monkeypatch, path, change):
    async def exercise():
        async with environment(tmp_path, monkeypatch) as env:
            entered, release = asyncio.Event(), asyncio.Event()

            async def wait_for_update():
                entered.set()
                await release.wait()
            env.state["before_reply"] = wait_for_update
            if path == "admin":
                task = asyncio.create_task(env.client.get("/api/admin/channels/1/oauth/usage"))
            else:
                task = asyncio.create_task(quota_refresher.QuotaRefresher()._refresh(1))
            await entered.wait()
            async with env.sessions() as db:
                row = await db.get(Channel, 1)
                if change == "delete":
                    await db.delete(row)
                else:
                    setattr(row, "key" if change == "key" else "type", "new-key" if change == "key" else "opencode")
                await db.commit()
            release.set()
            if path == "admin":
                assert (await task).status_code == 503
            else:
                with pytest.raises(Exception, match="configuration changed"):
                    await task
            assert usage_cache.fresh(1) is None
            async with env.sessions() as db:
                row = await db.get(Channel, 1)
                assert row is None if change == "delete" else row.enabled
    asyncio.run(exercise())


def test_local_mutation_fences_work_started_before_and_during_transaction():
    usage_cache._entries.clear()
    usage = ChannelUsageResponse(fetched_at=1)
    channel = SimpleNamespace(id=981, type="opencode", key="old")
    before = usage_cache.generation(channel.id)
    assert usage_cache.store(channel.id, usage, channel=channel)
    with usage_cache.mutation(channel.id):
        during = usage_cache.generation(channel.id)
        assert usage_cache.fresh(channel.id, channel=channel) is None
        assert not usage_cache.store(channel.id, usage, channel=channel, expected_generation=before)
        assert not usage_cache.store(channel.id, usage, channel=channel, expected_generation=during)
    assert not usage_cache.store(channel.id, usage, channel=channel, expected_generation=during)
    assert usage_cache.store(channel.id, usage, channel=channel)
    channel.key = "new"
    assert usage_cache.fresh(channel.id, channel=channel) is None
    usage_cache.invalidate(channel.id)


def test_old_opencode_inactive_never_disables_new_zhipu(tmp_path, monkeypatch):
    async def exercise():
        async with environment(tmp_path, monkeypatch) as env:
            async with env.sessions() as db:
                channel = await db.get(Channel, 1)
                from rotor.schemas.channel import OpenCodeUsage, OpenCodeSources
                usage = ChannelUsageResponse(subscription_status="inactive", fetched_at=1,
                    opencode=OpenCodeUsage(sources=OpenCodeSources(usage="error", subscription="ok", billing="error")))
                await quota_refresher.apply_subscription_status(db, channel, usage)
                assert channel.enabled is True
    asyncio.run(exercise())


def test_oauth_verified_using_token_actually_used_after_rotation(tmp_path, monkeypatch):
    async def exercise():
        async with environment(tmp_path, monkeypatch) as env:
            async with env.sessions() as db:
                row = await db.get(Channel, 1)
                row.auth_type = "oauth"
                row.oauth_account_id = "account"
                row.oauth_access_token = "old"
                row.oauth_refresh_token = "old-refresh"
                await db.commit()
                version = usage_cache.generation(1)
                identity = usage_cache.identity(row, include_oauth_tokens=False)
                row._quota_oauth_access_token = "rotated"
                row._quota_oauth_account_id = "account"
                async with env.sessions() as other:
                    await other.execute(update(Channel).where(Channel.id == 1).values(
                        oauth_access_token="rotated", oauth_refresh_token="rotated-refresh"))
                    await other.commit()
                await quota_refresher.verify_configuration(db, row, identity, version, oauth_fetched=True)
                row._quota_oauth_access_token = "superseded"
                with pytest.raises(Exception, match="configuration changed"):
                    await quota_refresher.verify_configuration(db, row, identity, version, oauth_fetched=True)
    asyncio.run(exercise())
