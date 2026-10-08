import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.core.admin_auth import AdminActor, require_admin_access
from rotor.database import get_db
from rotor.schemas.channel import ProbeModelsResponse
from rotor.schemas.channel_oauth import (
    OAuthCallbackRequest, OAuthCallbackResponse, OAuthSessionCreate, OAuthSessionResponse,
)
from rotor.services.chatgpt_oauth import (
    OAuthError, cancel_session, complete_session, parse_callback, probe_session_models, start_session,
)


router = APIRouter(prefix="/channels/oauth/sessions", tags=["channels"])


@router.post("", response_model=OAuthSessionResponse)
async def start_login(payload: OAuthSessionCreate, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    try:
        return await start_session(db, admin_user_id=actor.admin_user_id, channel_id=payload.channel_id)
    except OAuthError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.post("/{session_id}/parse", response_model=OAuthCallbackResponse)
async def parse_login(session_id: str, payload: OAuthCallbackRequest, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    try:
        return await parse_callback(db, session_id, actor.admin_user_id, payload.callback_url)
    except OAuthError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.post("/{session_id}/complete", response_model=OAuthSessionResponse)
async def complete_login(session_id: str, payload: OAuthCallbackRequest, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            return await complete_session(db, client, session_id, actor.admin_user_id, payload.callback_url)
    except OAuthError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.delete("/{session_id}", response_model=OAuthSessionResponse)
async def cancel_login(session_id: str, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    try:
        return await cancel_session(db, session_id, actor.admin_user_id)
    except OAuthError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.post("/{session_id}/probe-models", response_model=ProbeModelsResponse)
async def probe_login_models(session_id: str, actor: AdminActor = Depends(require_admin_access), db: AsyncSession = Depends(get_db)):
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            models, raw_count = await probe_session_models(db, client, session_id, actor.admin_user_id)
        return ProbeModelsResponse(
            models=models, latency_ms=int((time.perf_counter() - start) * 1000), raw_count=raw_count,
        )
    except OAuthError as exc:
        raise HTTPException(400 if exc.status_code == 401 else exc.status_code, str(exc)) from None
