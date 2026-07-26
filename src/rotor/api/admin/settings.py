from fastapi import APIRouter
from pydantic import BaseModel

from rotor.application_settings import (
    ApplicationSettings,
    RoutingSettings,
    application_settings,
)
from rotor.gateway.routing import routing_engine


router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsResponse(BaseModel):
    routing: RoutingSettings
    settings_path: str


def _response(settings: ApplicationSettings) -> SettingsResponse:
    return SettingsResponse(
        routing=settings.routing,
        settings_path=str(application_settings.path),
    )


@router.get("", response_model=SettingsResponse)
async def get_application_settings() -> SettingsResponse:
    return _response(application_settings.get())


@router.put("", response_model=SettingsResponse)
async def update_application_settings(
    updated: ApplicationSettings,
) -> SettingsResponse:
    saved = application_settings.save(updated)
    routing_engine.strategy = saved.routing.strategy
    routing_engine.configure_adaptive(saved.routing)
    return _response(saved)
