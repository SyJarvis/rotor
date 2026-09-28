import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

import rotor.api.v1.embeddings as endpoint
from rotor.api.v1.embeddings import EmbeddingRequest
from rotor.core.deps import get_current_token
from rotor.database import get_db
from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import RoutingEngine
from rotor.main import app


def channel(channel_id=1, **overrides):
    values = dict(id=channel_id, name=f"embedding-{channel_id}", type="openai",
                  protocol="openai", enabled=True, priority=10-channel_id, weight=1,
                  base_url="https://upstream.test/v1", key="provider-secret",
                  extra={"request_path": "/responses"},
                  model_mapping={"embed": "provider-embed"})
    values.update(overrides)
    return SimpleNamespace(**values)


class Database:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


class Accounting:
    extract_usage = AccountingService().extract_usage

    def __init__(self):
        self.successes = []
        self.failures = []
        self.attempts = []
        self.decisions = []

    def record_routing_decision(self, db, **kwargs):
        self.decisions.append(kwargs)

    async def record_success(self, db, **kwargs):
        self.successes.append(kwargs)

    async def record_failure(self, db, **kwargs):
        self.failures.append(kwargs)

    async def record(self, **kwargs):
        self.attempts.append(kwargs)
        kwargs['context'].recorded = True


def success(vector=None):
    return dict(object="list", model="provider-embed", data=[dict(object="embedding", index=0,
                embedding=vector if vector is not None else [0.25, 0.5])],
                usage=dict(prompt_tokens=3, total_tokens=3))


@pytest.fixture
def harness(monkeypatch):
    db = Database()
    accounting = Accounting()
    engine = RoutingEngine(strategy="fallback_order")
    state = SimpleNamespace(channels=[channel()], requests=[], responses=[], clients=[],
                            accounting=accounting, engine=engine, db=db, released=[])
    original_release = engine.release_attempt

    def release(admission):
        state.released.append(admission)
        original_release(admission)

    monkeypatch.setattr(engine, 'release_attempt', release)
    async def available(model, token, db):
        state.authorization = (model, token)
        return state.channels

    async def lease(*args, **kwargs):
        return SimpleNamespace(channel_id=None, reassessment_due=False)

    def upstream(request):
        state.requests.append(request)
        if state.responses:
            result = state.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            status, data = result
        else:
            status, data = 200, success()
            value = json.loads(request.content)['input']
            if isinstance(value, list) and not isinstance(value[0], int):
                data['data'] = [dict(data['data'][0], index=index) for index in range(len(value))]
        return httpx.Response(status, json=data)

    original_client = httpx.AsyncClient
    def client(**kwargs):
        value = original_client(transport=httpx.MockTransport(upstream), **kwargs)
        state.clients.append(value)
        return value

    monkeypatch.setattr(endpoint, 'get_available_channels', available)
    monkeypatch.setattr(endpoint, 'get_session_lease_preference', lease)
    monkeypatch.setattr(endpoint, 'routing_engine', engine)
    monkeypatch.setattr(endpoint, 'accounting_service', accounting)
    monkeypatch.setattr(endpoint, 'attempt_recorder', accounting)
    monkeypatch.setattr(endpoint, 'AsyncClient', client)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_token] = lambda: SimpleNamespace(id=7)

    async def request(payload=None, headers=None):
        async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://rotor") as client:
            return await client.post('/v1/embeddings', json=payload or dict(model='embed', input='hello'), headers=headers)

    state.request = request
    yield state
    app.dependency_overrides.clear()


@pytest.mark.parametrize('value', ['hello', ['hello', 'world'], [1, 2], [[1, 2], [3]]])
def test_four_inputs_preserved_and_model_mapped(harness, value):
    response = asyncio.run(harness.request(dict(model='embed', input=value, dimensions=2,
                                encoding_format='float', user='user1', provider_option='value')))
    payload = json.loads(harness.requests[0].content)
    assert response.status_code == 200
    assert payload == dict(model='provider-embed', input=value, dimensions=2,
                           encoding_format='float', user='user1', provider_option='value')
    assert str(harness.requests[0].url) == 'https://upstream.test/v1/embeddings'
    assert harness.requests[0].headers['authorization'] == 'Bearer provider-secret'
    assert response.headers['X-Rotor-Provider-Model'] == 'provider-embed'
    assert harness.authorization[1].id == 7
    assert harness.clients[0].is_closed
    assert harness.released[0].provider_succeeded
    usage = harness.accounting.successes[0]['usage']
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (3, 0, 3)
    assert harness.accounting.attempts[0]['request_protocol'] == 'openai_embeddings'
    assert harness.accounting.decisions[0]['required_capabilities'] == {'embeddings'}
    assert harness.db.commits == 1


@pytest.mark.parametrize('value', ['', [], [''], [[]], True, [True], [1.5], ['a', 1], [[1], 'a'], [-1], [[-1]]])
def test_invalid_input_rejected(value):
    with pytest.raises(ValidationError):
        EmbeddingRequest(model='embed', input=value)


@pytest.mark.parametrize('fields', [{'dimensions': 0}, {'dimensions': True}, {'dimensions': '2'},
                                    {'encoding_format': 'binary'}, {'model': ''}])
def test_invalid_options_rejected(harness, fields):
    response = asyncio.run(harness.request(dict(model='embed', input='hello') | fields))
    assert response.status_code == 422
    assert not harness.requests


def test_missing_auth_rejected(harness):
    del app.dependency_overrides[get_current_token]
    response = asyncio.run(harness.request())
    assert response.status_code == 401
    assert not harness.requests


def test_custom_path_and_auth(harness):
    harness.channels = [channel(extra={'embeddings_path': '/custom/embeddings', 'auth_type': 'x-api-key',
                                       'headers': {'X-Provider': 'custom'}})]
    response = asyncio.run(harness.request())
    assert response.status_code == 200
    assert str(harness.requests[0].url) == 'https://upstream.test/v1/custom/embeddings'
    assert harness.requests[0].headers['x-api-key'] == 'provider-secret'
    assert harness.requests[0].headers['x-provider'] == 'custom'


@pytest.mark.parametrize('protocol,provider,capabilities,expected', [
    ('openai', 'openai', None, 200), ('openai_responses', 'openai', ['embeddings'], 200),
    ('anthropic', 'anthropic', ['embeddings'], 503), ('openai', 'anthropic', None, 503),
    ('openai', 'openai', ['stream'], 503),
])
def test_compatible_candidates(harness, protocol, provider, capabilities, expected):
    extra = {} if capabilities is None else {'capabilities': capabilities}
    harness.channels = [channel(protocol=protocol, type=provider, extra=extra)]
    assert asyncio.run(harness.request()).status_code == expected
    assert len(harness.requests) == int(expected == 200)


@pytest.mark.parametrize('status', [401, 403, 404, 429, 503])
def test_retryable_failure_falls_back_and_releases(harness, status):
    harness.channels.append(channel(2))
    harness.responses = [(status, {'error': {'message': 'rate limit'}}), (200, success('AACAPwAAAEA='))]
    response = asyncio.run(harness.request(dict(model='embed', input='hello', encoding_format='base64')))
    assert response.status_code == 200
    assert response.json()['data'][0]['embedding'] == 'AACAPwAAAEA='
    assert response.headers['X-Rotor-Fallback'] == 'true'
    assert [a['outcome'] for a in harness.accounting.attempts] == ['failed', 'success']
    assert len(harness.released) == 2
    assert harness.accounting.failures[0]['request_protocol'] == 'openai_embeddings'


@pytest.mark.parametrize('status', [400, 422])
def test_nonretryable_failure_does_not_fallback(harness, status):
    harness.channels.append(channel(2))
    harness.responses = [(status, {'error': {'message': 'invalid request'}})]
    response = asyncio.run(harness.request())
    assert response.status_code != 200
    assert len(harness.requests) == 1
    assert len(harness.accounting.failures) == 1
    assert harness.clients[0].is_closed


@pytest.mark.parametrize('data', [[], {}, {'error': {'message': 'failed'}}, success([]),
                                 success() | {'usage': {'prompt_tokens': 'bad', 'total_tokens': 3}}])
def test_malformed_success_is_accounted_as_failure(harness, data):
    harness.responses = [(200, data)]
    response = asyncio.run(harness.request())
    assert response.status_code == 502
    assert not harness.accounting.successes
    assert len(harness.accounting.failures) == 1
    assert not harness.released[0].provider_succeeded
    assert harness.clients[0].is_closed


def test_admission_unavailable(harness, monkeypatch):
    monkeypatch.setattr(harness.engine, 'admit_attempt', lambda *args: None)
    assert asyncio.run(harness.request()).status_code == 503
    assert not harness.requests
    assert harness.clients[0].is_closed


def test_accounting_failure_still_closes_client(harness, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError('accounting unavailable')
    monkeypatch.setattr(harness.accounting, 'record_success', fail)
    with pytest.raises(RuntimeError, match='accounting unavailable'):
        asyncio.run(harness.request())
    assert harness.clients[0].is_closed
    assert len(harness.released) == 1


def test_sdk_default_base64(harness):
    openai = pytest.importorskip('openai')
    harness.responses = [(200, success('AACAPwAAAEA='))]
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http_client:
            client = openai.AsyncOpenAI(api_key='test', base_url='http://rotor/v1', http_client=http_client)
            return await client.embeddings.create(model='embed', input='hello')
    result = asyncio.run(run())
    assert result.data[0].embedding == [1.0, 2.0]


def test_incomplete_batch_is_not_success(harness):
    harness.responses = [(200, success())]
    response = asyncio.run(harness.request(dict(model='embed', input=['first', 'second'])))
    assert response.status_code == 502
    assert not harness.accounting.successes
    assert harness.accounting.attempts[0]['error'].fallback_allowed is False


def test_real_channel_restrictions_and_accounting(harness, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from rotor.core.deps import get_available_channels
    from rotor.database import Base
    from rotor.gateway.attempts import AttemptRecorder
    from rotor.models.channel import Channel
    from rotor.models.request_attempt import RequestAttempt
    from rotor.models.token import Token
    from rotor.models.usage import UsageLedger

    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                provider = Channel(name='embeddings', type='openai', protocol='openai',
                                   key='secret', base_url='https://upstream.test/v1',
                                   models=['embed'], model_mapping={'embed': 'provider-embed'}, extra={})
                token = Token(name='test', key='test', allowed_channels=[999], quota=100)
                db.add_all([provider, token])
                await db.commit()
                app.dependency_overrides[get_db] = lambda: db
                app.dependency_overrides[get_current_token] = lambda: token
                monkeypatch.setattr(endpoint, 'get_available_channels', get_available_channels)
                monkeypatch.setattr(endpoint, 'accounting_service', AccountingService())
                monkeypatch.setattr(endpoint, 'attempt_recorder', AttemptRecorder())
                denied = await harness.request()
                assert denied.status_code == 404
                assert not harness.requests
                token.allowed_channels = [provider.id]
                await db.commit()
                response = await harness.request(headers={'X-Conversation-Id': 'embedding-session'})
                assert response.status_code == 200
                ledger = (await db.execute(select(UsageLedger))).scalar_one()
                attempt = (await db.execute(select(RequestAttempt))).scalar_one()
                await db.refresh(token)
                assert (ledger.prompt_tokens, ledger.completion_tokens, ledger.total_tokens) == (3, 0, 3)
                assert (ledger.model, ledger.provider_model) == ('embed', 'provider-embed')
                assert ledger.request_protocol == 'openai_embeddings'
                assert ledger.conversation_id == 'embedding-session'
                assert attempt.outcome == 'success'
                assert token.used_quota == 3
                assert token.request_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_duplicate_indices_are_not_success(harness):
    data = success()
    data['data'] *= 2
    harness.responses = [(200, data)]
    response = asyncio.run(harness.request(dict(model='embed', input=['first', 'second'])))
    assert response.status_code == 502
    assert not harness.accounting.successes


def test_transport_timeout_falls_back(harness):
    harness.channels.append(channel(2))
    harness.responses = [httpx.ReadTimeout('timed out'), (200, success())]
    response = asyncio.run(harness.request())
    assert response.status_code == 200
    assert response.headers['X-Rotor-Fallback'] == 'true'
    assert len(harness.accounting.failures) == 1
    assert len(harness.released) == 2
    assert harness.clients[0].is_closed
