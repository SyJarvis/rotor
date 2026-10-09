import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from alembic import command
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.api import users
from rotor.api.admin.auth import router as auth_router
from rotor.api.admin.tokens import router as admin_tokens_router
from rotor.api.v1.models import router as models_router
from rotor.core.admin_auth import ADMIN_CSRF_COOKIE, ensure_default_admin, require_admin_access
from rotor.core.deps import get_available_channels, get_current_token
from rotor.core.user_access import accessible_models, ensure_base_token, validate_token_access
from rotor.database import Base, get_db
from rotor.gateway.accounting import AccountingService, UsageData
from rotor.models.admin_auth import AdminAuthEvent, AdminSession, AdminUser, UserInvitation
from rotor.models.channel import Channel
from rotor.models.log import RequestLog
from rotor.models.token import Token
from rotor.models.usage import UsageLedger


class Harness:
    async def __aenter__(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.admin = await ensure_default_admin(db)
            self.admin.must_change_password = False
            db.add_all([Channel(id=1,name='one',type='openai',key='private',base_url='https://one.invalid',models=['one','alias']),
                        Channel(id=2,name='two',type='openai',key='private',base_url='https://two.invalid',models=['two'])])
            await db.commit()
        self.app = FastAPI()
        self.app.include_router(auth_router, prefix='/api')
        for router in (users.admin_router, users.auth_router, users.me_router, users.personal_logs_router):
            self.app.include_router(router, prefix='/api')
        self.app.include_router(admin_tokens_router,prefix='/api/admin',dependencies=[Depends(require_admin_access)])
        self.app.include_router(users.log_router,prefix='/api/admin',dependencies=[Depends(require_admin_access)])
        self.app.include_router(models_router,prefix='/v1')
        async def override_db():
            async with self.sessions() as db:
                try:
                    yield db
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
        self.app.dependency_overrides[get_db] = override_db
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app),base_url='http://test')
        await self.login('admin','123456')
        return self

    async def __aexit__(self,*args):
        await self.client.aclose()
        await self.engine.dispose()

    async def login(self,username,password):
        response = await self.client.post('/api/auth/login',json={'username':username,'password':password})
        assert response.status_code == 200, response.text
        self.client.headers['X-CSRF-Token'] = self.client.cookies[ADMIN_CSRF_COOKIE]
        return response.json()

    async def create_user(self,name='alice',**kwargs):
        response = await self.client.post('/api/admin/users',json={'username':name,'password':'initial-password',
            'quota':1000,'allowed_channels':[1],**kwargs})
        assert response.status_code == 201,response.text
        return response.json()

    async def use_user(self,name='alice'):
        await self.login(name,'initial-password')
        response = await self.client.post('/api/auth/change-password',json={'current_password':'initial-password','new_password':'updated-password'})
        assert response.status_code == 200,response.text
        return response.json()


def run(coro):
    return asyncio.run(coro)


def test_new_account_base_default_and_personal_permissions():
    async def scenario():
        async with Harness() as h:
            user = await h.create_user()
            assert user['role'] == 'user' and user['default_token_id']
            assert datetime.fromisoformat(user['created_at']).utcoffset() == timedelta(0)
            async with h.sessions() as db:
                keys = (await db.scalars(select(Token).where(Token.owner_id==user['id']))).all()
                assert len(keys)==1 and keys[0].is_base and keys[0].name=='base'
                assert h.admin.quota is None and h.admin.allowed_channels is None
            actor = await h.use_user()
            assert actor['user_id']==user['id'] and actor['role']=='user'
            assert (await h.client.get('/api/admin/users')).status_code==403
            assert (await h.client.patch('/api/me',json={'role':'admin'})).status_code==422
            assert (await h.client.patch('/api/me',json={'username':'other'})).status_code==422
            renamed=await h.client.patch('/api/me',json={'display_name':'Alice'})
            assert renamed.json()['display_name']=='Alice'
            assert renamed.json()['display_timezone']
            assert datetime.fromisoformat(renamed.json()['last_login_at']).utcoffset() == timedelta(0)
            keys=(await h.client.get('/api/me/tokens')).json()
            assert len(keys)==1 and keys[0]['is_default']
            assert datetime.fromisoformat(keys[0]['created_at']).utcoffset() == timedelta(0)
            assert (await h.client.post('/api/me/tokens',json={'name':'bad','owner_id':h.admin.id})).status_code==422
            assert (await h.client.post('/api/me/tokens',json={'name':'bad','allowed_channels':[2]})).status_code==403
            assert (await h.client.post('/api/me/tokens',json={'name':'bad','quota':2000})).status_code==403
            assert (await h.client.get('/v1/models',headers={'Authorization':f"Bearer {keys[0]['key']}"})).json()['data'][0]['id']=='alias'
    run(scenario())


def test_owned_key_lifecycle_default_and_old_key_revocation():
    async def scenario():
        async with Harness() as h:
            user=await h.create_user()
            await h.use_user()
            original=(await h.client.get('/api/me/tokens')).json()[0]
            response=await h.client.post('/api/me/tokens',json={'name':'second','expire_time':'2099-01-01T08:00:00+08:00'})
            assert response.status_code==201,response.text
            second=response.json()
            assert datetime.fromisoformat(second['expire_time']) == datetime(2099,1,1,tzinfo=timezone.utc)
            assert (await h.client.delete(f"/api/me/tokens/{original['id']}")).status_code==409
            rotated=(await h.client.post(f"/api/me/tokens/{second['id']}/rotate")).json()
            assert rotated['key']!=second['key']
            async with h.sessions() as db:
                assert await db.scalar(select(Token).where(Token.key==second['key'])) is None
            assert (await h.client.patch(f"/api/me/tokens/{original['id']}",json={'enabled':False})).status_code==200
            assert (await h.client.post(f"/api/me/tokens/{second['id']}/default")).status_code==200
            assert (await h.client.delete(f"/api/me/tokens/{original['id']}")).status_code==204
            async with h.sessions() as db:
                account=await db.get(AdminUser,user['id'])
                await ensure_base_token(db,account)
                assert await db.scalar(select(func.count(Token.id)).where(Token.owner_id==account.id))==1
                assert account.default_token_id==second['id']
    run(scenario())


def test_cross_user_key_access_is_not_found():
    async def scenario():
        async with Harness() as h:
            alice=await h.create_user('alice');bob=await h.create_user('bob')
            await h.use_user('alice')
            for method,path,body in [('PATCH',f"/api/me/tokens/{bob['default_token_id']}",{'name':'stolen'}),
                ('POST',f"/api/me/tokens/{bob['default_token_id']}/default",{}),
                ('POST',f"/api/me/tokens/{bob['default_token_id']}/rotate",{}),('DELETE',f"/api/me/tokens/{bob['default_token_id']}",{})]:
                response=await h.client.request(method,path,json=body)
                assert response.status_code==404,response.text
    run(scenario())


def test_disable_role_change_and_password_reset_revoke_sessions_and_keys():
    async def scenario():
        async with Harness() as h:
            user=await h.create_user();await h.use_user()
            user_cookie=dict(h.client.cookies)
            async with h.sessions() as db:
                key=await db.get(Token,user['default_token_id'])
                await validate_token_access(key,db)
            await h.login('admin','123456')
            assert (await h.client.patch(f"/api/admin/users/{user['id']}",json={'enabled':False})).status_code==200
            async with h.sessions() as db:
                with pytest.raises(HTTPException):
                    await validate_token_access(key,db)
                assert not (await db.scalars(select(AdminSession).where(AdminSession.admin_user_id==user['id']))).all()
            assert (await h.client.patch(f"/api/admin/users/{user['id']}",json={'enabled':True})).status_code==200
            async with httpx.AsyncClient(transport=httpx.ASGITransport(h.app),base_url='http://test',cookies=user_cookie) as old:
                assert (await old.get('/api/auth/me')).status_code==401
            await h.login('alice','updated-password')
            await h.login('admin','123456')
            assert (await h.client.patch(f"/api/admin/users/{user['id']}",json={'role':'admin'})).status_code==200
            async with h.sessions() as db:
                assert not (await db.scalars(select(AdminSession).where(AdminSession.admin_user_id==user['id']))).all()
            assert (await h.client.post(f"/api/admin/users/{user['id']}/reset-password",json={'password':'reset-pass'})).status_code==200
            actor=await h.login('alice','reset-pass');assert actor['must_change_password']
    run(scenario())


def test_last_administrator_is_protected():
    async def scenario():
        async with Harness() as h:
            for changes in ({'enabled':False},{'role':'user'}):
                response=await h.client.patch(f'/api/admin/users/{h.admin.id}',json=changes)
                assert response.status_code==409,response.text
            other=await h.create_user('other',role='admin')
            assert (await h.client.patch(f'/api/admin/users/{h.admin.id}',json={'role':'user'})).status_code==200
            assert (await h.client.get('/api/admin/users')).status_code==401
    run(scenario())


def test_invitation_is_one_time_transactional_and_cannot_escalate():
    async def scenario():
        async with Harness() as h:
            invitation=(await h.client.post('/api/admin/invitations',json={'quota':88,'allowed_channels':[2]})).json()
            assert datetime.fromisoformat(invitation['expires_at']).utcoffset() == timedelta(0)
            listed=(await h.client.get('/api/admin/invitations')).json()
            assert 'token' not in listed[0] and 'secret_hash' not in listed[0]
            assert (await h.client.post('/api/admin/invitations',json={'role':'admin'})).status_code==422
            payload={'invite_token':invitation['token'],'username':'invited','password':'new-password'}
            escalated=await h.client.post('/api/auth/register',json={**payload,'role':'admin'})
            assert escalated.status_code==422
            response=await h.client.post('/api/auth/register',json=payload)
            assert response.status_code==201,response.text
            actor=response.json();assert actor['role']=='user' and not actor['must_change_password']
            assert (await h.client.post('/api/auth/register',json={**payload,'username':'replay'})).status_code==400
            async with h.sessions() as db:
                user=await db.get(AdminUser,actor['id'])
                assert user.quota==88 and user.allowed_channels==[2]
                assert await db.scalar(select(func.count(Token.id)).where(Token.owner_id==user.id))==1
                invitation_row=await db.get(UserInvitation,invitation['id'])
                assert invitation_row.used_by==user.id
                events=(await db.scalars(select(AdminAuthEvent.event_type))).all()
                assert 'invitation_registered' in events and 'invitation_created' in events
    run(scenario())


def test_failed_username_registration_does_not_consume_invitation():
    async def scenario():
        async with Harness() as h:
            invite=(await h.client.post('/api/admin/invitations',json={})).json()
            payload={'invite_token':invite['token'],'username':'admin','password':'new-password'}
            assert (await h.client.post('/api/auth/register',json=payload)).status_code==409
            assert (await h.client.post('/api/auth/register',json={**payload,'username':'fresh'})).status_code==201
    run(scenario())


def test_expired_revoked_invites_and_invalid_invite_do_not_hash(monkeypatch):
    async def scenario():
        async with Harness() as h:
            invite=(await h.client.post('/api/admin/invitations',json={})).json()
            assert (await h.client.delete(f"/api/admin/invitations/{invite['id']}")).status_code==204
            def never_hash(password):
                raise AssertionError('Invalid invitation must be checked before hashing')
            monkeypatch.setattr(users,'hash_admin_password',never_hash)
            response=await h.client.post('/api/auth/register',json={'invite_token':invite['token'],'username':'fresh','password':'new-password'})
            assert response.status_code==400
    run(scenario())


def test_quota_totals_and_permissions_are_account_scoped():
    async def scenario():
        async with Harness() as h:
            user=await h.create_user(quota=10,allowed_channels=[1],allowed_models=['one'])
            await h.use_user()
            second=(await h.client.post('/api/me/tokens',json={'name':'second','allowed_channels':[]})).json()
            async with h.sessions() as db:
                base=await db.get(Token,user['default_token_id']);other=await db.get(Token,second['id'])
                assert await accessible_models(base,db)==['one']
                assert await accessible_models(other,db)==[]
                assert [c.id for c in await get_available_channels('one',base,db)]==[1]
                with pytest.raises(Exception):
                    await get_available_channels('two',base,db)
                service=AccountingService()
                await service._update_token_counters(db,base,UsageData(total_tokens=6))
                await service._update_token_counters(db,other,UsageData(total_tokens=5))
                await db.commit()
                with pytest.raises(HTTPException,match='Account token quota'):
                    await validate_token_access(base,db)
                owner=await db.get(AdminUser,user['id'],populate_existing=True)
                assert owner.used_quota==11
    run(scenario())


def test_personal_logs_scope_every_aggregate_and_detail_after_key_deletion():
    async def scenario():
        async with Harness() as h:
            user=await h.create_user();other=await h.create_user('bob')
            async with h.sessions() as db:
                for owner,token,count in [(user['id'],9999,7),(other['id'],other['default_token_id'],90),(None,None,100)]:
                    db.add(RequestLog(owner_id=owner,token_id=token,channel_id=1,model='one',request_model='one',
                        prompt_tokens=count,total_tokens=count,completion_tokens=0,success=True,cached_tokens=0,
                        capacity_snapshot={'secret':'provider-data'},response_body={'secret':'provider-data'}))
                await db.commit()
            await h.use_user()
            logs=(await h.client.get('/api/me/logs')).json()
            assert len(logs)==1 and logs[0]['total_tokens']==7
            assert 'capacity_snapshot' not in logs[0]
            assert (await h.client.get('/api/me/logs/count')).json()=={'count':1}
            assert (await h.client.get('/api/me/logs/stats')).json()['total_tokens']==7
            assert sum(r['tokens'] for r in (await h.client.get('/api/me/logs/timeseries_by_model')).json())==7
            assert (await h.client.get('/api/me/logs/models')).json()[0]['total_tokens']==7
            assert (await h.client.get('/api/me/logs/channels')).json()[0]['total_requests']==1
            assert (await h.client.get('/api/me/logs/2')).status_code==404
            detail=(await h.client.get('/api/me/logs/1')).json()
            assert 'response_body' not in detail
            assert (await h.client.get('/api/me/logs',params={'token_id':other['default_token_id']})).json()==[]
    run(scenario())


def test_new_log_and_ledger_write_immutable_owner_snapshot():
    async def scenario():
        async with Harness() as h:
            user=await h.create_user();await h.use_user()
            async with h.sessions() as db:
                token=await db.get(Token,user['default_token_id']);channel=await db.get(Channel,1)
                await AccountingService().record_success(db,request_id='owner-test',conversation_id='conv',request_protocol='openai_chat',
                    token=token,channel=channel,model='one',provider_model='one',usage=UsageData(prompt_tokens=4,total_tokens=4),latency_ms=10,client_ip='local')
                await db.commit()
                assert (await db.scalar(select(RequestLog))).owner_id==user['id']
                assert (await db.scalar(select(UsageLedger))).owner_id==user['id']
    run(scenario())


def test_personal_logs_hide_upstream_error_text_but_keep_admin_diagnostics():
    from rotor.core.exceptions import format_error_message

    async def scenario():
        credential = 'synthetic-provider-credential'
        request = httpx.Request('POST', 'https://provider.invalid/v1/chat/completions')
        response = httpx.Response(401, request=request,
            json={'error': {'message': f'Incorrect API key: {credential}'}})
        error = httpx.HTTPStatusError('Upstream rejected credential', request=request, response=response)
        diagnostic = format_error_message(error)
        assert credential in diagnostic
        async with Harness() as h:
            alice = await h.create_user()
            bob = await h.create_user('bob')
            async with h.sessions() as db:
                rows = [RequestLog(owner_id=user['id'], token_id=user['default_token_id'], channel_id=1,
                    model='one', request_model='one', prompt_tokens=0, completion_tokens=0, total_tokens=0,
                    cached_tokens=0, success=False, error_code=type(error).__name__, error_message=diagnostic)
                    for user in (alice, bob)]
                db.add_all(rows)
                await db.commit()
                alice_log, bob_log = (row.id for row in rows)
            await h.use_user()
            listing = await h.client.get('/api/me/logs')
            detail = await h.client.get(f'/api/me/logs/{alice_log}')
            assert listing.status_code == detail.status_code == 200
            assert len(listing.json()) == 1
            assert listing.json()[0]['id'] == alice_log
            for payload in (listing.json()[0], detail.json()):
                assert 'error_message' not in payload
                assert payload['error_code'] == 'HTTPStatusError'
                assert payload['success'] is False
            assert credential not in listing.text and credential not in detail.text
            assert (await h.client.get(f'/api/me/logs/{bob_log}')).status_code == 404
            assert (await h.client.get('/api/admin/logs')).status_code == 403
            await h.login('admin', '123456')
            admin_listing = await h.client.get('/api/admin/logs')
            admin_detail = await h.client.get(f'/api/admin/logs/{alice_log}')
            assert admin_listing.status_code == admin_detail.status_code == 200
            assert len(admin_listing.json()) == 2
            assert all(row['error_message'] == diagnostic for row in admin_listing.json())
            assert admin_detail.json()['error_message'] == diagnostic
    run(scenario())


def test_schema_upgrade_preserves_legacy_credentials_sessions_and_unowned_data(tmp_path):
    from rotor.migrations.runner import _alembic_config, run_startup_migrations
    database=tmp_path/'upgrade.db';url=f'sqlite+aiosqlite:///{database}'
    config=_alembic_config(url)
    command.upgrade(config,'e1f2a3b4c5d6')
    async def populate():
        engine=create_async_engine(url)
        async with engine.begin() as db:
            await db.execute(text("INSERT INTO admin_users(id,username,password_hash,enabled,must_change_password) VALUES(1,'legacy','unchanged-hash',1,0)"))
            await db.execute(text("INSERT INTO admin_sessions(admin_user_id,secret_hash,csrf_hash,last_seen_at,expires_at) VALUES(1,'old-session','old-csrf',CURRENT_TIMESTAMP,'2099-01-01')"))
            await db.execute(text("INSERT INTO tokens(id,key,name,user_id,used_quota,enabled,expired,[group],request_count,token_count) VALUES(1,'legacy-key','old','arbitrary-tag',9,1,0,'default',0,0)"))
            await db.execute(text("INSERT INTO request_logs(token_id,model,request_model,prompt_tokens,completion_tokens,total_tokens,success,cached_tokens) VALUES(1,'one','one',0,0,0,1,0)"))
        await engine.dispose()
    run(populate());run_startup_migrations(url)
    async def verify():
        engine=create_async_engine(url);sessions=async_sessionmaker(engine,expire_on_commit=False)
        async with sessions() as db:
            admin=await ensure_default_admin(db)
            assert admin.id==1 and admin.password_hash=='unchanged-hash' and admin.role=='admin'
            assert admin.quota is None and admin.allowed_channels is None
            legacy=await db.get(Token,1)
            assert legacy.key=='legacy-key' and legacy.user_id=='arbitrary-tag' and legacy.owner_id is None
            assert (await db.scalar(select(RequestLog))).owner_id is None
            assert (await db.scalar(select(AdminSession))).secret_hash=='old-session'
            assert await db.scalar(select(func.count(Token.id)).where(Token.owner_id==1,Token.is_base.is_(True)))==1
            await ensure_default_admin(db)
            assert await db.scalar(select(func.count(Token.id)))==2
        await engine.dispose()
    run(verify())


def test_fresh_migration_and_admin_base_seed(tmp_path):
    from rotor.migrations.runner import run_startup_migrations
    url=f'sqlite+aiosqlite:///{tmp_path / "fresh.db"}'
    run_startup_migrations(url)
    async def verify():
        engine=create_async_engine(url);sessions=async_sessionmaker(engine,expire_on_commit=False)
        async with sessions() as db:
            admin=await ensure_default_admin(db)
            assert admin.role=='admin' and admin.default_token_id
            assert (await db.get(Token,admin.default_token_id)).owner_id==admin.id
        await engine.dispose()
    run(verify())


def test_concurrent_demotion_preserves_one_enabled_administrator(tmp_path):
    from rotor.core.admin_auth import AdminActor
    from starlette.requests import Request
    async def scenario():
        engine=create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "admins.db"}')
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add_all([AdminUser(id=i,username=f'admin{i}',password_hash='hash',role='admin',enabled=True,must_change_password=False) for i in (1,2)])
            await db.commit()
        async def demote(user_id):
            async with sessions() as db:
                try:
                    await users.update_user(user_id,users.UserPatch(role='user'),
                        Request({'type':'http','headers':[],'client':('127.0.0.1',1)}),
                        AdminActor(f'admin-user-{user_id}',f'admin{user_id}','session',False,user_id,0),db)
                    return 200
                except HTTPException as exc:
                    await db.rollback()
                    return exc.status_code
        assert sorted(await asyncio.gather(demote(1),demote(2)))==[200,409]
        async with sessions() as db:
            assert await db.scalar(select(func.count(AdminUser.id)).where(AdminUser.role=='admin',AdminUser.enabled.is_(True)))==1
        await engine.dispose()
    run(scenario())


def test_concurrent_account_usage_increments_do_not_lose_tokens(tmp_path):
    async def scenario():
        engine=create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "quota.db"}')
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(AdminUser(id=1,username='user',password_hash='hash',role='user',enabled=True,must_change_password=False,quota=100))
            await db.flush()
            db.add_all([Token(id=i,key=f'test-key-{i}',name=f'key{i}',owner_id=1) for i in (1,2)])
            await db.commit()
        async def count(token_id):
            async with sessions() as db:
                token=await db.get(Token,token_id)
                await AccountingService()._update_token_counters(db,token,UsageData(total_tokens=3))
                await db.commit()
        await asyncio.gather(*(count(i%2+1) for i in range(10)))
        async with sessions() as db:
            assert (await db.get(AdminUser,1)).used_quota==30
            assert (await db.get(Token,1)).used_quota==15
            assert (await db.get(Token,2)).used_quota==15
        await engine.dispose()
    run(scenario())


def test_fresh_schema_matches_declared_model_metadata(tmp_path):
    from rotor.migrations.runner import _alembic_config, run_startup_migrations
    url=f'sqlite+aiosqlite:///{tmp_path / "parity.db"}'
    run_startup_migrations(url)
    command.check(_alembic_config(url))
