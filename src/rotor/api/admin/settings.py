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
    display_timezone: str
    settings_path: str


class ConversationStoreStatus(BaseModel):
    enabled: bool
    worker_running: bool
    queue_size: int
    queue_maxsize: int
    dropped_queue_full: int
    dropped_retry_exhausted: int


def _response(settings: ApplicationSettings) -> SettingsResponse:
    return SettingsResponse(
        routing=settings.routing,
        display_timezone=settings.display_timezone,
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


@router.get("/conversation-store", response_model=ConversationStoreStatus)
async def get_conversation_store_status() -> ConversationStoreStatus:
    """Runtime health of the conversation archive worker and its drop counters."""
    from rotor.api.v1.chat import conversation_store

    return ConversationStoreStatus(**conversation_store.status())
