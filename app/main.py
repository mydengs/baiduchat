from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api.openai import router as openai_router
from app.core.config import get_settings
from app.db.init_db import init_db
from app.services.logging_service import setup_logging
from app.web.admin import router as admin_router


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()
    init_db()
    app = FastAPI(title=settings.app_name)
    app.include_router(openai_router, prefix="/v1", tags=["openai"])
    app.include_router(admin_router, prefix="/admin", tags=["admin"])
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


app = create_app()
