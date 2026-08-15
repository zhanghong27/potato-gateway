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

    idle_labels = {
        "researcher": "等着查资料",
        "creator": "等着做视频",
        "critic": "等着审片",
        "engineer": "守着系统",
    }
    working_labels = {
        "researcher": "搜集视频素材",
        "creator": "生成或修改视频",
        "critic": "审查视频",
        "engineer": "处理系统故障",
    }

    def observed_agent(agent_id: str) -> tuple[str, str, str]:
        heartbeat = heartbeat_by_agent.get(agent_id)
        if not heartbeat:
            if hub_status == "online":
                return "offline", "offline", "Runner 未连接"
            return "unknown", "unknown", "状态暂时未知"
        try:
            observed = datetime.fromisoformat(str(heartbeat.get("updated_at") or ""))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        except ValueError:
            return "unknown", "unknown", "状态暂时未知"
        if age > 120:
            return "offline", "offline", "Runner 已离线"
        raw = str(heartbeat.get("status") or "online")
        current_id = str(heartbeat.get("current_work_item_id") or "")
        metadata = heartbeat.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        mode = str(metadata.get("mode") or "")
        if raw == "error":
            return "error", "error", "上次任务异常"
        if mode == "calibration_review" or current_id.startswith("calreview_"):
            return "calibrating", "calibrating", "校准视频评审"
        if mode == "calibration" or current_id.startswith("caljob_"):
            return "calibrating", "calibrating", "执行校准复测"
        if raw in {"busy", "working"} or current_id.startswith("work_"):
            return "busy", "working", working_labels[agent_id]
        if raw in {"calibrating", "preparing_review", "reviewing"}:
            return "calibrating", "calibrating", "执行校准任务"
        return "online", "idle", idle_labels[agent_id]

    def agent_info(agent_id: str, display_name: str) -> AgentInfo:
        observed_status, activity_state, activity_label = observed_agent(agent_id)
        return AgentInfo(
            id=agent_id,
            display_name=display_name,
            status=observed_status,
            activity_state=activity_state,
            activity_label=activity_label,
        )

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
            agent_info("researcher", "薯博士"),
            agent_info("creator", "清蒸土豆"),
            agent_info("critic", "酸辣土豆丝"),
            agent_info("engineer", "薯码宝贝"),
        ],
    )
