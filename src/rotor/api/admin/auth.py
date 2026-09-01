import asyncio
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from rotor.config import settings
from rotor.core.admin_auth import (
    ADMIN_CSRF_COOKIE,
    ADMIN_SESSION_COOKIE,
    AdminActor,
    admin_authentication_error,
    create_admin_session,
    find_admin_user,
    hash_admin_session_secret,
    hash_admin_password,
    require_admin_access,
    require_admin_actor,
    verify_admin_password,
)
from rotor.database import get_db
from rotor.models.admin_auth import AdminAuthEvent, AdminSession, AdminUser
from rotor.schemas.admin_auth import (
    AdminActorResponse,
    AdminAuthEventResponse,
    AdminLoginRequest,
    AdminPasswordChangeRequest,
)
from rotor.services.admin_security import (
    admin_request_context,
    clear_login_failures,
    login_retry_after_seconds,
    normalize_admin_username,
    record_admin_auth_event,
    record_login_failure,
)


router = APIRouter(prefix="/auth", tags=["admin-auth"])


def _actor_response(actor: AdminActor) -> AdminActorResponse:
    return AdminActorResponse(
        actor_id=actor.actor_id,
        username=actor.username,
        auth_method=actor.auth_method,
        must_change_password=actor.must_change_password,
    )


def _set_session_cookies(
    response: Response,
    *,
    session_secret: str,
    csrf_secret: str,
) -> None:
    cookie_options = {
        "max_age": settings.ROTOR_ADMIN_SESSION_TTL_SECONDS,
        "secure": settings.ROTOR_ADMIN_COOKIE_SECURE,
        "samesite": "strict",
        "path": "/",
    }
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        session_secret,
        httponly=True,
        **cookie_options,
    )
    response.set_cookie(
        ADMIN_CSRF_COOKIE,
        csrf_secret,
        httponly=False,
        **cookie_options,
    )
    response.headers["Cache-Control"] = "no-store"


def _login_rate_limit_error(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many administrator login attempts",
        headers={"Retry-After": str(retry_after)},
    )


@router.post("/login", response_model=AdminActorResponse)
async def login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AdminActorResponse:
    context = admin_request_context(request)
    username_key = normalize_admin_username(payload.username)
    retry_after = await login_retry_after_seconds(
        db,
        username_key=username_key,
        client_ip=context.client_ip,
    )
    if retry_after is not None:
        raise _login_rate_limit_error(retry_after)

    user = await find_admin_user(db, payload.username)
    valid = user is not None and await asyncio.to_thread(
        verify_admin_password, payload.password, user.password_hash
    )
    if not valid or user is None:
        retry_after = await record_login_failure(
            db,
            username_key=username_key,
            client_ip=context.client_ip,
        )
        record_admin_auth_event(
            db,
            admin_user_id=user.id if user is not None else None,
            username=payload.username,
            event_type="login_failed",
            success=False,
            reason="invalid_credentials",
            context=context,
        )
        if retry_after is not None:
            record_admin_auth_event(
                db,
                admin_user_id=user.id if user is not None else None,
                username=payload.username,
                event_type="login_rate_limited",
                success=False,
                reason="lockout_started",
                context=context,
            )
        await db.commit()
        if retry_after is not None:
            raise _login_rate_limit_error(retry_after)
        raise admin_authentication_error()

    await clear_login_failures(
        db,
        username_key=username_key,
        client_ip=context.client_ip,
    )
    user.last_login_at = datetime.now(timezone.utc)
    session, session_secret, csrf_secret = await create_admin_session(
        db, admin_user=user
    )
    record_admin_auth_event(
        db,
        admin_user_id=user.id,
        username=user.username,
        event_type="login_succeeded",
        success=True,
        context=context,
        session_id=session.id,
    )
    await db.commit()
    _set_session_cookies(
        response,
        session_secret=session_secret,
        csrf_secret=csrf_secret,
    )
    return _actor_response(AdminActor(
        actor_id=f"admin-user-{user.id}",
        username=user.username,
        auth_method="session",
        must_change_password=user.must_change_password,
        admin_user_id=user.id,
        session_id=session.id,
    ))


@router.get("/me", response_model=AdminActorResponse)
async def me(
    actor: AdminActor = Depends(require_admin_actor),
) -> AdminActorResponse:
    return _actor_response(actor)


@router.get(
    "/audit-events",
    response_model=list[AdminAuthEventResponse],
)
async def list_auth_audit_events(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    _actor: AdminActor = Depends(require_admin_access),
    db: AsyncSession = Depends(get_db),
) -> list[AdminAuthEvent]:
    result = await db.scalars(
        select(AdminAuthEvent)
        .order_by(AdminAuthEvent.created_at.desc(), AdminAuthEvent.id.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.all())


@router.post("/change-password", response_model=AdminActorResponse)
async def change_password(
    payload: AdminPasswordChangeRequest,
    request: Request,
    actor: AdminActor = Depends(require_admin_actor),
    db: AsyncSession = Depends(get_db),
) -> AdminActorResponse:
    context = admin_request_context(request)
    user = await db.get(AdminUser, actor.admin_user_id)
    if user is None or not await asyncio.to_thread(
        verify_admin_password, payload.current_password, user.password_hash
    ):
        record_admin_auth_event(
            db,
            admin_user_id=user.id if user is not None else None,
            username=actor.username,
            event_type="password_change_failed",
            success=False,
            reason="invalid_current_password",
            context=context,
            session_id=actor.session_id,
        )
        await db.commit()
        raise admin_authentication_error()
    if payload.current_password == payload.new_password:
        record_admin_auth_event(
            db,
            admin_user_id=user.id,
            username=user.username,
            event_type="password_change_failed",
            success=False,
            reason="password_reused",
            context=context,
            session_id=actor.session_id,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New administrator password must be different",
        )

    user.password_hash = await asyncio.to_thread(
        hash_admin_password, payload.new_password
    )
    user.must_change_password = False
    await db.execute(
        delete(AdminSession).where(
            AdminSession.admin_user_id == user.id,
            AdminSession.id != actor.session_id,
        )
    )
    record_admin_auth_event(
        db,
        admin_user_id=user.id,
        username=user.username,
        event_type="password_changed",
        success=True,
        context=context,
        session_id=actor.session_id,
    )
    await db.commit()
    return AdminActorResponse(
        actor_id=actor.actor_id,
        username=user.username,
        auth_method="session",
        must_change_password=False,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    context = admin_request_context(request)
    session_secret = request.cookies.get(ADMIN_SESSION_COOKIE)
    if session_secret:
        result = await db.execute(
            select(AdminSession, AdminUser)
            .join(AdminUser, AdminUser.id == AdminSession.admin_user_id)
            .where(
                AdminSession.secret_hash
                == hash_admin_session_secret(session_secret)
            )
        )
        row = result.one_or_none()
        if row is not None:
            session, user = row
            record_admin_auth_event(
                db,
                admin_user_id=user.id,
                username=user.username,
                event_type="logout",
                success=True,
                context=context,
                session_id=session.id,
            )
            await db.delete(session)
            await db.commit()

    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    response.delete_cookie(ADMIN_CSRF_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
