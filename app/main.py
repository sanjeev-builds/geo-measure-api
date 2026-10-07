from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.api import files
from app.config import Settings
from app.db import build_engine, build_session_factory, get_db, init_database
from app.middleware import MaxBodySizeMiddleware
from app.schemas import HealthOut

# Allowance for multipart boundaries and part headers on top of the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = build_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        init_database(engine)
        yield
        engine.dispose()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = build_session_factory(engine)

    app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES)
    app.include_router(files.router)

    @app.get("/health", response_model=HealthOut, tags=["health"])
    def health(db: Session = Depends(get_db)) -> HealthOut:
        db.execute(text("SELECT 1"))
        return HealthOut(status="ok", database="ok")

    return app


app = create_app()
