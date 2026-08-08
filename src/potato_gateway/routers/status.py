from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from potato_gateway import __version__
from potato_gateway.adapters import HubClient, HubUnavailableError
from potato_gateway.auth import require_bearer_token
from potato_gateway.models import (
    AgentInfo,
    HealthResponse,
    PotatoHubInfo,
    ServiceInfo,
    StatusResponse,
)


router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    operation_id="getGatewayHealth",
    tags=["health"],
)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/api/status",
    response_model=StatusResponse,
    operation_id="getPotatoSystemStatus",
    dependencies=[Depends(require_bearer_token)],
    tags=["status"],
)
def status(request: Request) -> StatusResponse:
    settings = request.app.state.settings
    client = HubClient(
        settings.hub_url,
        token=settings.resolved_hub_token(),
        timeout=settings.hub_timeout_seconds,
    )
    hub_status = "offline"
    hub_message = "Potato Hub is not reachable"
    heartbeat_by_agent: dict[str, dict] = {}
    try:
        client.health()
        agent_payload = client.request("GET", "/api/agents")
        heartbeat_by_agent = {
            str(item.get("agent_id")): item
            for item in agent_payload.get("heartbeats", [])
            if isinstance(item, dict)
        }
        hub_status = "online"
        hub_message = "Potato Hub is reachable"
    except HubUnavailableError:
        pass

    def observed_status(agent_id: str) -> str:
        heartbeat = heartbeat_by_agent.get(agent_id)
        if not heartbeat:
            return "offline" if hub_status == "online" else "unknown"
        try:
            observed = datetime.fromisoformat(str(heartbeat.get("updated_at") or ""))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        except ValueError:
            return "unknown"
        if age > 120:
            return "offline"
        raw = str(heartbeat.get("status") or "online")
        return raw if raw in {"online", "busy", "calibrating", "error"} else "online"

    return StatusResponse(
        service=ServiceInfo(
            name="potato-gateway",
            status="running",
            version=__version__,
        ),
        potato_hub=PotatoHubInfo(
            status=hub_status,
            message=hub_message,
        ),
        agents=[
            AgentInfo(id="researcher", display_name="薯博士", status=observed_status("researcher")),
            AgentInfo(id="creator", display_name="清蒸土豆", status=observed_status("creator")),
            AgentInfo(id="critic", display_name="酸辣土豆丝", status=observed_status("critic")),
            AgentInfo(id="engineer", display_name="薯码宝贝", status=observed_status("engineer")),
        ],
    )
