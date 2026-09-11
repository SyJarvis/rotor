"""Version facts exposed to clients and declared for packaging."""

import asyncio
import tomllib
from pathlib import Path

from httpx import ASGITransport, AsyncClient

import rotor
from rotor.config import settings
from rotor.main import app as rotor_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_settings_version_matches_package_version() -> None:
    assert settings.VERSION == rotor.__version__


def test_package_version_matches_pyproject() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

    assert metadata["project"]["version"] == rotor.__version__


def test_api_endpoint_reports_package_version() -> None:
    async def scenario() -> None:
        transport = ASGITransport(app=rotor_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api")

        assert response.status_code == 200
        assert response.json()["version"] == rotor.__version__

    asyncio.run(scenario())
