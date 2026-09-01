import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rotor.database import Base
from rotor.models.channel import Channel
from rotor.models.response_route import ResponseRoute
from rotor.models.token import Token
from rotor.services.response_routes import get_response_route, save_response_route


def test_response_route_is_persistent_token_scoped_and_updatable() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as db:
            request_started_at = datetime(
                2026,
                8,
                19,
                0,
                30,
                tzinfo=timezone.utc,
            )
            channel = Channel(
                name="responses",
                type="openai",
                key="upstream-key",
                base_url="https://example.com/v1",
                models=["test-model"],
                model_mapping={},
                protocol="openai_responses",
                extra={},
            )
            owner = Token(key="sk-owner", name="owner")
            other = Token(key="sk-other", name="other")
            db.add_all([channel, owner, other])
            await db.flush()

            route = await save_response_route(
                db,
                response_id="resp_1",
                token_id=owner.id,
                channel_id=channel.id,
                conversation_id="conv_1",
                model="test-model",
                status="queued",
                request_started_at=request_started_at,
            )
            await save_response_route(
                db,
                response_id="resp_1",
                token_id=owner.id,
                channel_id=channel.id,
                status="completed",
                usage_accounted=True,
            )
            await db.commit()

            assert route.status == "completed"
            assert route.usage_accounted is True
            assert route.created_at == request_started_at
            assert (await get_response_route(db, "resp_1", owner.id)).channel_id == channel.id
            assert await get_response_route(db, "resp_1", other.id) is None

        await engine.dispose()

    asyncio.run(exercise())
