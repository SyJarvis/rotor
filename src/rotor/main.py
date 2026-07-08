import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from rotor.config import settings
from rotor.database import init_db
from rotor.core.exceptions import (
    APIRouterException,
    api_router_exception_handler,
    general_exception_handler,
)
from rotor.core.middleware import LoggingMiddleware
from rotor.api.v1 import chat, models, anthropic, responses
from rotor.api.admin import channels, tokens, logs
from rotor.models.conversation import ConversationRecord
from rotor.models.usage import UsageLedger

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
FRONTEND_DIRS = [
    Path(__file__).resolve().parents[2] / "frontend",
    Path(__file__).resolve().parent / "frontend",
]
FRONTEND_DIR = next((path for path in FRONTEND_DIRS if path.exists()), FRONTEND_DIRS[0])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("Starting Rotor...")
    await init_db()
    logger.info("Database initialized")

    yield

    # Shutdown
    logger.info("Shutting down Rotor...")


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
app.add_exception_handler(Exception, general_exception_handler)


@app.get("/api")
async def api_root():
    """API metadata endpoint."""
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "running",
        "endpoints": {
            "openai": "/v1/chat/completions",
            "anthropic": "/anthropic/v1/messages",
            "models": "/v1/models",
            "admin": "/api/admin",
            "admin_ui": "/",
        }
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/")
async def frontend_index():
    """Serve the admin frontend index."""
    return FileResponse(FRONTEND_DIR / "index.html")


# Include routers
app.include_router(chat.router, prefix=settings.API_V1_STR, tags=["chat"])
app.include_router(responses.router, prefix=settings.API_V1_STR, tags=["responses"])
app.include_router(models.router, prefix=settings.API_V1_STR, tags=["models"])
# Anthropic-compatible endpoint
app.include_router(anthropic.router, prefix="/anthropic/v1", tags=["anthropic"])

# Admin routes
app.include_router(channels.router, prefix="/api/admin")
app.include_router(tokens.router, prefix="/api/admin")
app.include_router(logs.router, prefix="/api/admin")

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


# Startup event for backward compatibility
@app.on_event("startup")
async def startup_event():
    """Startup event handler (deprecated, use lifespan instead)."""
    await init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "rotor.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=settings.LOG_LEVEL.lower()
    )
