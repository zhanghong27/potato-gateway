from __future__ import annotations

from fastapi import APIRouter, Depends

from potato_gateway import __version__
from potato_gateway.auth import require_bearer_token
from potato_gateway.models import (
    AgentInfo,
    HealthResponse,
    PotatoHubInfo,
    ServiceInfo,
    StatusResponse,
)


router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/api/status",
    response_model=StatusResponse,
    dependencies=[Depends(require_bearer_token)],
    tags=["status"],
)
def status() -> StatusResponse:
    return StatusResponse(
        service=ServiceInfo(
            name="potato-gateway",
            status="running",
            version=__version__,
        ),
        potato_hub=PotatoHubInfo(
            status="unknown",
            message="Potato Hub integration is not configured yet",
        ),
        agents=[
            AgentInfo(id="researcher", display_name="薯博士", status="unknown"),
            AgentInfo(id="creator", display_name="清蒸土豆", status="unknown"),
            AgentInfo(id="critic", display_name="酸辣土豆丝", status="unknown"),
        ],
    )
