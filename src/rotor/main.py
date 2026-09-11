import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from httpx import HTTPStatusError, RequestError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from rotor.config import settings
from rotor.database import init_db
from rotor.core.exceptions import (
    APIRouterException,
    api_router_exception_handler,
    general_exception_handler,
    UpstreamProtocolError,
    UpstreamOverloaded,
)
from rotor.core.anthropic_errors import (
    protocol_http_exception_handler,
    protocol_validation_exception_handler,
    protocol_upstream_exception_handler,
)
from rotor.core.middleware import LoggingMiddleware
from rotor.core.logging_config import LOG_FORMAT, configure_file_logging
from rotor.api.v1 import chat, images, models, anthropic, responses
from rotor.api.v1 import anthropic_models
from rotor.api.admin import (
    auth as admin_auth,
    channels,
    mcp_control_keys,
    tokens,
    logs,
    mindagent,
    monitoring,
    settings as admin_settings,
)
from rotor.api.control import channels as control_channels
from rotor.api.control import requests as control_requests
from rotor.core.control_auth import (
    ControlAPIException,
    control_api_exception_handler,
)
from rotor.core.admin_auth import require_admin_access
# Model imports register tables in Base.metadata during application startup.
from rotor.models.conversation import ConversationRecord  # noqa: F401
from rotor.models.usage import UsageLedger  # noqa: F401
from rotor.models.response_route import ResponseRoute  # noqa: F401
from rotor.models.routing_decision import RoutingDecisionRecord  # noqa: F401
from rotor.models.request_attempt import RequestAttempt  # noqa: F401
from rotor.models.mcp_control_key import MCPControlKey  # noqa: F401
from rotor.models.session_lease import (  # noqa: F401
    SessionLease,
    SessionLeaseEvent,
)
from rotor.models.admin_auth import (  # noqa: F401
    AdminAuthEvent,
    AdminLoginThrottle,
    AdminSession,
    AdminUser,
)

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format=LOG_FORMAT,
)
logger = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    configure_file_logging(settings.ROTOR_LOG_DIR, settings.LOG_LEVEL)
    logger.info("Starting Rotor...")
    await init_db()
    logger.info("Database initialized")

    # Start conversation store background worker
    from rotor.api.v1.chat import conversation_store

    conversation_store.attach()

    yield

    # Shutdown
    logger.info("Shutting down Rotor...")
    await conversation_store.shutdown()


# Create FastAPI application
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.LOG_LEVEL == "DEBUG" else None,
    redoc_url="/redoc" if settings.LOG_LEVEL == "DEBUG" else None,
)


# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom logging middleware
app.add_middleware(LoggingMiddleware)


# Exception handlers
app.add_exception_handler(APIRouterException, api_router_exception_handler)
app.add_exception_handler(StarletteHTTPException, protocol_http_exception_handler)
app.add_exception_handler(RequestValidationError, protocol_validation_exception_handler)
app.add_exception_handler(HTTPStatusError, protocol_upstream_exception_handler)
app.add_exception_handler(RequestError, protocol_upstream_exception_handler)
app.add_exception_handler(UpstreamProtocolError, protocol_upstream_exception_handler)
app.add_exception_handler(UpstreamOverloaded, protocol_upstream_exception_handler)
app.add_exception_handler(
    ControlAPIException,
    control_api_exception_handler,
)
app.add_exception_handler(Exception, general_exception_handler)


@app.get("/anthropic/api/hello")
async def anthropic_api_hello():
    """Anthropic-compatible connectivity probe."""
    return {"message": "hello"}


@app.head("/anthropic/api/hello", include_in_schema=False)
async def anthropic_api_hello_head():
    """Answer HEAD probes without adding a duplicate OpenAPI operation."""
    return {"message": "hello"}


@app.get("/api")
async def api_root():
    """API metadata endpoint."""
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "running",
        "endpoints": {
            "openai": "/v1/chat/completions",
            "responses": "/v1/responses",
            "images": "/v1/images/generations",
            "anthropic": "/anthropic/v1/messages",
            "models": "/v1/models",
            "admin": "/api/admin",
            "control": "/api/control/v1",
            "admin_ui": "/",
        }
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "protocols": [
            "openai_chat",
            "openai_responses",
            "anthropic_messages",
            "openai_images",
        ],
    }


@app.get("/")
async def frontend_index():
    """Serve the admin frontend index."""
    return FileResponse(FRONTEND_DIR / "index.html")


# Include routers
app.include_router(chat.router, prefix=settings.API_V1_STR, tags=["chat"])
app.include_router(responses.router, prefix=settings.API_V1_STR, tags=["responses"])
app.include_router(images.router, prefix=settings.API_V1_STR, tags=["images"])
app.include_router(models.router, prefix=settings.API_V1_STR, tags=["models"])
# Anthropic-compatible endpoint
app.include_router(anthropic.router, prefix="/anthropic/v1", tags=["anthropic"])
app.include_router(anthropic_models.router, prefix="/anthropic/v1", tags=["anthropic"])

# Admin routes
app.include_router(admin_auth.router, prefix="/api/admin")
admin_dependencies = [Depends(require_admin_access)]
app.include_router(
    channels.router,
    prefix="/api/admin",
    dependencies=admin_dependencies,
)
app.include_router(
    tokens.router,
    prefix="/api/admin",
    dependencies=admin_dependencies,
)
app.include_router(
    mcp_control_keys.router,
    prefix="/api/admin",
    dependencies=admin_dependencies,
)
app.include_router(
    logs.router,
    prefix="/api/admin",
    dependencies=admin_dependencies,
)
app.include_router(
    monitoring.router,
    prefix="/api/admin",
    dependencies=admin_dependencies,
)
app.include_router(
    admin_settings.router,
    prefix="/api/admin",
    dependencies=admin_dependencies,
)
app.include_router(
    mindagent.router,
    prefix="/api/admin",
    dependencies=admin_dependencies,
)

# Independently authenticated Control API.
app.include_router(control_channels.router, prefix="/api/control/v1")
app.include_router(control_requests.router, prefix="/api/control/v1")

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "rotor.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level=settings.LOG_LEVEL.lower()
    )
