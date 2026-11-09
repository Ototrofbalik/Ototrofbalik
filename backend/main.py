"""FastAPI giriş noktası: `uvicorn backend.main:app`."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api import register_error_handlers, router
from backend.database import init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="FactoryFlow AI", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
