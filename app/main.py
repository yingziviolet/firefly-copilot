from fastapi import FastAPI

from app.api.routes_health import router as health_router
from app.api.routes_upload import router as upload_router
from app.api.routes_webhook import router as webhook_router
from app.config import get_settings
from app.logger import setup_logging


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(title="Firefly Copilot", version="0.1.0")
    app.include_router(health_router)
    app.include_router(upload_router, prefix="/api")
    app.include_router(webhook_router, prefix="/api")
    return app


app = create_app()
