from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from potato_gateway.calibration_ui import CALIBRATION_HTML
from potato_gateway.home_ui import HOME_HTML

from potato_gateway.config import Settings, get_settings
from potato_gateway.routers import (
    agents_router,
    calibrations_router,
    prompt_versions_router,
    status_router,
    workflows_router,
)


LOGGER_NAME = "potato_gateway"
ACTION_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "gpt-action-openapi.yaml"
ACTION_SCHEMA_ROUTE = "/potato-actions-v0.2.5.yaml"
LEGACY_ACTION_SCHEMA_ROUTE = "/potato-actions-v0.2.4.yaml"
ACTION_SCHEMA_BUILD = "historical-submissions-3.1-20260808"


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
        version="0.2.0",
        debug=False,
        lifespan=lifespan,
    )
    if settings is not None:
        app.state.settings = settings
        configure_logging(settings.log_level)
    app.include_router(status_router)
    app.include_router(agents_router)
    app.include_router(calibrations_router)
    app.include_router(prompt_versions_router)
    app.include_router(workflows_router)

    @app.get("/", include_in_schema=False)
    async def home_console() -> HTMLResponse:
        return HTMLResponse(HOME_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/calibrations", include_in_schema=False)
    async def calibration_console() -> HTMLResponse:
        return HTMLResponse(CALIBRATION_HTML, headers={"Cache-Control": "no-store"})

    @app.get(LEGACY_ACTION_SCHEMA_ROUTE, include_in_schema=False)
    @app.get(ACTION_SCHEMA_ROUTE, include_in_schema=False)
    async def get_action_schema() -> PlainTextResponse:
        return PlainTextResponse(
            ACTION_SCHEMA_PATH.read_text(encoding="utf-8"),
            media_type="application/yaml",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Potato-Schema-Build": ACTION_SCHEMA_BUILD,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request_handler(
        _request: Request, _exception: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid request"},
        )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started_at = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        identifiers = " ".join(
            f"{name}={value}"
            for name in ("session_id", "agent_id", "turn_id")
            if (value := getattr(request.state, name, None)) is not None
        )
        logging.getLogger(LOGGER_NAME).info(
            "%s %s %s %.2fms%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            f" {identifiers}" if identifiers else "",
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
