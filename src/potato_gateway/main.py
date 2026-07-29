from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request

from potato_gateway.config import Settings, get_settings
from potato_gateway.routers import status_router


LOGGER_NAME = "potato_gateway"


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or get_settings()
        app.state.settings = resolved_settings
        configure_logging(resolved_settings.log_level)
        yield

    app = FastAPI(
        title="Potato Gateway",
        version="0.1.0",
        debug=False,
        lifespan=lifespan,
    )
    if settings is not None:
        app.state.settings = settings
        configure_logging(settings.log_level)
    app.include_router(status_router)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started_at = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logging.getLogger(LOGGER_NAME).info(
            "%s %s %s %.2fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "potato_gateway.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


app = create_app()
