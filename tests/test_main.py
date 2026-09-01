import asyncio

from httpx import ASGITransport, AsyncClient, Response

from rotor.main import app


def _request(method: str, path: str) -> Response:
    async def exercise() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.request(method, path)

    return asyncio.run(exercise())


def test_anthropic_hello_get_returns_compatible_payload() -> None:
    response = _request("GET", "/anthropic/api/hello")

    assert response.status_code == 200
    assert response.json() == {"message": "hello"}


def test_anthropic_hello_head_returns_success_without_body() -> None:
    response = _request("HEAD", "/anthropic/api/hello")

    assert response.status_code == 200
    assert response.content == b""
