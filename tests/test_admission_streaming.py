import asyncio
from types import SimpleNamespace

import anyio
import httpx
import pytest
from starlette.requests import ClientDisconnect

from rotor.gateway.routing import RoutingEngine
from rotor.gateway.streaming import AdmissionStreamingResponse


SCOPE = {"type": "http", "asgi": {"spec_version": "2.4"}}


async def receive():
    await asyncio.Event().wait()


class UpstreamStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b"first"
        yield b"second"

    async def aclose(self):
        await anyio.sleep(0)
        self.closed = True


async def resources():
    engine = RoutingEngine()
    channel = SimpleNamespace(id=1, enabled=True, extra={})
    admission = engine.admit_attempt("model", channel)
    other = engine.admit_attempt("model", channel)
    upstream_stream = UpstreamStream()

    async def handler(request):
        return httpx.Response(200, stream=upstream_stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = await client.send(client.build_request("GET", "https://upstream.example"), stream=True)
    state = SimpleNamespace(started=False, finalized=False)

    async def body():
        state.started = True
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            state.finalized = True

    async def close_upstream():
        assert admission.released
        await upstream.aclose()

    return SimpleNamespace(
        engine=engine, channel=channel, admission=admission, other=other,
        upstream=upstream, client=client, upstream_stream=upstream_stream,
        body=body(), state=state, close_callbacks=(close_upstream, client.aclose),
    )


def assert_released_and_closed(env):
    assert env.admission.released
    stats = env.engine.adaptive._stats[("model", env.channel.id)]
    assert stats.inflight == 1
    env.engine.release_attempt(env.admission)
    assert stats.inflight == 1
    assert not env.other.released
    assert env.upstream.is_closed
    assert env.upstream_stream.closed
    assert env.client.is_closed
    env.engine.release_attempt(env.other)
    assert stats.inflight == 0


def test_normal_completion_closes_resources_and_releases_only_its_admission():
    async def scenario():
        env = await resources()
        sent = []

        async def send(message):
            sent.append(message)

        await AdmissionStreamingResponse(
            env.body, admission=env.admission, close_callbacks=env.close_callbacks,
        )(SCOPE, receive, send)
        assert env.state.started and env.state.finalized
        assert [message.get("body") for message in sent[1:]] == [b"first", b"second", b""]
        assert_released_and_closed(env)

    asyncio.run(scenario())


def test_cancelled_probe_restarts_cooldown_without_releasing_older_healthy_attempt():
    async def scenario():
        env = await resources()
        now = [0.0]
        env.engine._clock = lambda: now[0]
        env.engine.release_attempt(env.admission)
        env.engine.mark_unavailable("model", env.channel, 10)
        now[0] = 10.0
        probe = env.engine.admit_attempt("model", env.channel)
        assert probe is not None and probe.is_probe
        env.admission = probe

        async def send(message):
            raise asyncio.CancelledError("probe client disconnected")

        with pytest.raises(asyncio.CancelledError, match="probe client disconnected"):
            await AdmissionStreamingResponse(
                env.body, admission=probe, close_callbacks=env.close_callbacks,
            )(SCOPE, receive, send)
        state = env.engine._cooldowns[("model", env.channel.id)]
        assert state.probe is None
        assert state.deadline == 40.0
        assert env.engine.adaptive._stats[("model", env.channel.id)].inflight == 1
        assert env.engine.admit_attempt("model", env.channel) is None
        now[0] = 40.0
        next_probe = env.engine.admit_attempt("model", env.channel)
        assert next_probe is not None and next_probe.is_probe
        assert env.engine.adaptive._stats[("model", env.channel.id)].inflight == 2
        env.engine.release_attempt(next_probe)
        assert_released_and_closed(env)

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_start_failure_closes_precreated_resources_without_starting_generator(error_type):
    async def scenario():
        env = await resources()
        original_error = error_type("send start failed")

        async def send(message):
            assert message["type"] == "http.response.start"
            raise original_error

        with pytest.raises(error_type) as captured:
            await AdmissionStreamingResponse(
                env.body, admission=env.admission, close_callbacks=env.close_callbacks,
            )(SCOPE, receive, send)
        assert captured.value is original_error
        assert not env.state.started
        assert not env.state.finalized
        assert_released_and_closed(env)

    asyncio.run(scenario())


def test_disconnect_after_first_chunk_closes_suspended_generator():
    async def scenario():
        env = await resources()

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        with pytest.raises(ClientDisconnect):
            await AdmissionStreamingResponse(
                env.body, admission=env.admission, close_callbacks=env.close_callbacks,
            )(SCOPE, receive, send)
        assert env.state.started and env.state.finalized
        assert_released_and_closed(env)

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [None, RuntimeError, asyncio.CancelledError])
def test_cleanup_failures_do_not_skip_remaining_callbacks_or_replace_response_error(error_type, caplog):
    class BrokenCloseIterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            raise RuntimeError("iterator close failed")

    async def scenario():
        env = await resources()
        original_error = error_type("original response error") if error_type else None

        async def broken_callback():
            raise asyncio.CancelledError("callback cancelled")

        async def send(message):
            if original_error is not None:
                raise original_error

        response = AdmissionStreamingResponse(
            BrokenCloseIterator(), admission=env.admission,
            close_callbacks=(broken_callback,) + env.close_callbacks,
        )
        if error_type is None:
            await response(SCOPE, receive, send)
        else:
            with pytest.raises(error_type) as captured:
                await response(SCOPE, receive, send)
            assert captured.value is original_error
        assert_released_and_closed(env)
        await env.body.aclose()

    asyncio.run(scenario())
    assert sum("Failed to close streaming response resource" in record.message for record in caplog.records) == 2


def test_cancelled_anyio_scope_still_finishes_all_cleanup():
    async def scenario():
        env = await resources()
        propagated = False

        async def send(message):
            await anyio.sleep(0)

        with anyio.CancelScope() as cancel_scope:
            cancel_scope.cancel()
            try:
                await AdmissionStreamingResponse(
                    env.body, admission=env.admission, close_callbacks=env.close_callbacks,
                )(SCOPE, receive, send)
            except asyncio.CancelledError:
                propagated = True
                raise
        assert propagated
        assert not env.state.started
        assert_released_and_closed(env)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_response", [False, True])
def test_repeated_task_cancellation_waits_for_cleanup_and_preserves_original_cancel(cancel_response):
    async def scenario():
        env = await resources()
        response_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_allowed = asyncio.Event()

        async def send(message):
            response_started.set()
            if cancel_response:
                await asyncio.Event().wait()

        async def paused_close():
            assert env.admission.released
            cleanup_started.set()
            await cleanup_allowed.wait()
            await env.close_callbacks[0]()

        response = AdmissionStreamingResponse(
            env.body, admission=env.admission,
            close_callbacks=(paused_close, env.client.aclose),
        )
        task = asyncio.create_task(response(SCOPE, receive, send))
        await asyncio.wait_for(response_started.wait(), timeout=2)
        if cancel_response:
            task.cancel("original disconnect")
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        task.cancel("cancel during cleanup")
        await asyncio.sleep(0)
        task.cancel("second cleanup cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        cleanup_allowed.set()
        message = "original disconnect" if cancel_response else "cancel during cleanup"
        with pytest.raises(asyncio.CancelledError, match=message):
            await task
        assert_released_and_closed(env)

    asyncio.run(scenario())


@pytest.mark.parametrize("spec_version", ["2.0", "2.3"])
def test_legacy_asgi_disconnect_listener_releases_and_closes_resources(spec_version):
    async def scenario():
        env = await resources()

        async def disconnected():
            return {"type": "http.disconnect"}

        async def send(message):
            await anyio.sleep(0)

        await AdmissionStreamingResponse(
            env.body, admission=env.admission, close_callbacks=env.close_callbacks,
        )({"type": "http", "asgi": {"spec_version": spec_version}}, disconnected, send)
        assert_released_and_closed(env)

    asyncio.run(scenario())
