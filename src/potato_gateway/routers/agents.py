from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from potato_gateway.adapters import (
    AgentNotRegisteredError,
    HermesProfileSourceError,
)
from potato_gateway.auth import require_bearer_token
from potato_gateway.database import DatabaseUnavailableError, get_app_database
from potato_gateway.models import AgentProfileResponse, ErrorResponse
from potato_gateway.repositories import CalibrationStateSourceError
from potato_gateway.services import (
    AgentProfileService,
    AgentProfileUnavailableError,
    build_agent_profile_service,
)


AGENT_IDS = ["researcher", "creator", "critic", "engineer"]
router = APIRouter()


def get_agent_profile_service(request: Request) -> AgentProfileService:
    settings = request.app.state.settings
    try:
        database = get_app_database(
            request.app,
            path=settings.database_path,
            hermes_home=settings.hermes_home,
        )
        return build_agent_profile_service(settings, database)
    except (
        DatabaseUnavailableError,
        HermesProfileSourceError,
        CalibrationStateSourceError,
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent profile data is temporarily unavailable",
        ) from None


@router.get(
    "/api/agents/{agent_id}/profile",
    response_model=AgentProfileResponse,
    operation_id="getAgentProfile",
    dependencies=[Depends(require_bearer_token)],
    tags=["agents"],
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Agent is not registered"},
        503: {"model": ErrorResponse, "description": "Agent data is unavailable"},
    },
)
def get_agent_profile(
    agent_id: Annotated[
        str,
        Path(
            description="Registered Potato Agent ID",
            json_schema_extra={"enum": AGENT_IDS},
        ),
    ],
    request: Request,
    service: Annotated[AgentProfileService, Depends(get_agent_profile_service)],
) -> AgentProfileResponse:
    try:
        profile = service.get_profile(agent_id)
    except AgentNotRegisteredError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        ) from None
    except AgentProfileUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent profile data is temporarily unavailable",
        ) from None

    request.state.agent_id = profile.agent.id
    if profile.calibration.latest_session_id is not None:
        request.state.session_id = profile.calibration.latest_session_id
    return profile
