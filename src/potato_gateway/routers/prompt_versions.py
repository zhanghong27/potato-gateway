from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from potato_gateway.adapters import AgentNotRegisteredError, HermesProfileAdapter, HermesProfileSourceError
from potato_gateway.auth import require_bearer_token
from potato_gateway.database import DatabaseUnavailableError, get_app_database
from potato_gateway.models import (
    CreatePromptCandidateRequest,
    ErrorResponse,
    PromotePromptVersionRequest,
    PromptVersionListResponse,
    PromptVersionSummary,
)
from potato_gateway.repositories import (
    CalibrationReviewRepository,
    PromptVersionConflictError,
    PromptVersionNotFoundError,
    PromptVersionRepository,
)
from potato_gateway.services import PromptVersionService, PromptVersionServiceUnavailableError


AGENT_IDS = ["researcher", "creator", "critic"]
router = APIRouter()


def get_prompt_version_service(request: Request) -> PromptVersionService:
    settings = request.app.state.settings
    try:
        database = get_app_database(
            request.app,
            path=settings.database_path,
            hermes_home=settings.hermes_home,
        )
        return PromptVersionService(
            PromptVersionRepository(database),
            HermesProfileAdapter(settings.hermes_home, settings.agent_registry_path),
            CalibrationReviewRepository(database),
        )
    except (DatabaseUnavailableError, HermesProfileSourceError):
        raise HTTPException(status_code=503, detail="Prompt version data is unavailable") from None


@router.post(
    "/api/agents/{agent_id}/prompt-versions",
    response_model=PromptVersionSummary,
    status_code=status.HTTP_201_CREATED,
    operation_id="createPromptCandidate",
    dependencies=[Depends(require_bearer_token)],
    tags=["prompt-versions"],
    responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def create_prompt_candidate(
    agent_id: Annotated[str, Path(json_schema_extra={"enum": AGENT_IDS})],
    payload: CreatePromptCandidateRequest,
    response: Response,
    service: Annotated[PromptVersionService, Depends(get_prompt_version_service)],
) -> PromptVersionSummary:
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        version, created = service.create_candidate(agent_id, payload)
    except AgentNotRegisteredError:
        raise HTTPException(status_code=404, detail="Agent not found") from None
    except PromptVersionConflictError:
        raise HTTPException(status_code=409, detail="Prompt version idempotency conflict") from None
    except PromptVersionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Prompt version data is unavailable") from None
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return version


@router.get(
    "/api/agents/{agent_id}/prompt-versions",
    response_model=PromptVersionListResponse,
    operation_id="listPromptVersions",
    dependencies=[Depends(require_bearer_token)],
    tags=["prompt-versions"],
)
def list_prompt_versions(
    agent_id: Annotated[str, Path(json_schema_extra={"enum": AGENT_IDS})],
    service: Annotated[PromptVersionService, Depends(get_prompt_version_service)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> PromptVersionListResponse:
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return service.list_versions(agent_id, limit)
    except PromptVersionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Prompt version data is unavailable") from None


@router.post(
    "/api/admin/agents/{agent_id}/prompt-versions/{prompt_version_id}/promote",
    response_model=PromptVersionSummary,
    operation_id="promotePromptVersionAdmin",
    dependencies=[Depends(require_bearer_token)],
    include_in_schema=False,
)
def promote_prompt_version(
    agent_id: Annotated[str, Path(json_schema_extra={"enum": AGENT_IDS})],
    prompt_version_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: PromotePromptVersionRequest,
    service: Annotated[PromptVersionService, Depends(get_prompt_version_service)],
) -> PromptVersionSummary:
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return service.promote(agent_id, prompt_version_id, payload.confirm_content_sha256)
    except PromptVersionNotFoundError:
        raise HTTPException(status_code=404, detail="Prompt version not found") from None
    except PromptVersionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except PromptVersionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Prompt promotion failed") from None


@router.post(
    "/api/admin/agents/{agent_id}/prompt-versions/{prompt_version_id}/testing",
    response_model=PromptVersionSummary,
    operation_id="markPromptVersionTestingAdmin",
    dependencies=[Depends(require_bearer_token)],
    include_in_schema=False,
)
def mark_prompt_version_testing(
    agent_id: Annotated[str, Path(json_schema_extra={"enum": AGENT_IDS})],
    prompt_version_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[PromptVersionService, Depends(get_prompt_version_service)],
) -> PromptVersionSummary:
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        return service.mark_testing(agent_id, prompt_version_id)
    except PromptVersionNotFoundError:
        raise HTTPException(status_code=404, detail="Prompt version not found") from None
    except PromptVersionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except PromptVersionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Prompt testing state update failed") from None


@router.post(
    "/api/admin/agents/{agent_id}/prompt-versions/{prompt_version_id}/rollback",
    response_model=PromptVersionSummary,
    operation_id="rollbackPromptVersionAdmin",
    dependencies=[Depends(require_bearer_token)],
    include_in_schema=False,
)
def rollback_prompt_version(
    agent_id: Annotated[str, Path(json_schema_extra={"enum": AGENT_IDS})],
    prompt_version_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: PromotePromptVersionRequest,
    service: Annotated[PromptVersionService, Depends(get_prompt_version_service)],
) -> PromptVersionSummary:
    return promote_prompt_version(agent_id, prompt_version_id, payload, service)
