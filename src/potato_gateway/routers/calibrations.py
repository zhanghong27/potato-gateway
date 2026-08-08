from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)

from potato_gateway.adapters import AgentNotRegisteredError, HermesProfileSourceError, HubClient, HubNotFoundError, HubUnavailableError
from potato_gateway.auth import require_bearer_token
from potato_gateway.database import DatabaseUnavailableError, get_app_database
from potato_gateway.models import (
    AgentCalibrationListResponse,
    CalibrationSessionResponse,
    CalibrationSessionSummary,
    CalibrationEvidenceResponse,
    CalibrationExecutionResponse,
    CalibrationExecutionListResponse,
    CalibrationReviewListResponse,
    CalibrationReviewResponse,
    CalibrationTurnResponse,
    CreateCalibrationSessionRequest,
    CreateCalibrationReviewRequest,
    ErrorResponse,
    ExecuteCalibrationTurnRequest,
    RecordCalibrationTurnRequest,
)
from potato_gateway.repositories import (
    CalibrationSessionNotFoundError,
    CalibrationSessionNotWritableError,
    CalibrationSessionRepository,
    CalibrationStateSourceError,
    CalibrationTurnConflictError,
    CalibrationExecutionConflictError,
    CalibrationExecutionNotFoundError,
    CalibrationExecutionRepository,
    CalibrationReviewConflictError,
    CalibrationReviewNotFoundError,
    CalibrationReviewRepository,
)
from potato_gateway.services import (
    AgentProfileUnavailableError,
    CalibrationService,
    CalibrationServiceUnavailableError,
    CalibrationExecutionService,
    CalibrationExecutionServiceUnavailableError,
    CalibrationReviewService,
    CalibrationReviewServiceUnavailableError,
    build_agent_profile_service,
)


AGENT_IDS = ["researcher", "creator", "critic"]
router = APIRouter()


def get_calibration_service(request: Request) -> CalibrationService:
    settings = request.app.state.settings
    try:
        database = get_app_database(
            request.app,
            path=settings.database_path,
            hermes_home=settings.hermes_home,
        )
        repository = CalibrationSessionRepository(database)
        profile_service = build_agent_profile_service(settings, database)
        return CalibrationService(repository, profile_service)
    except (
        DatabaseUnavailableError,
        HermesProfileSourceError,
        CalibrationStateSourceError,
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Calibration data is temporarily unavailable",
        ) from None


def get_calibration_execution_service(request: Request) -> CalibrationExecutionService:
    settings = request.app.state.settings
    try:
        database = get_app_database(
            request.app,
            path=settings.database_path,
            hermes_home=settings.hermes_home,
        )
        return CalibrationExecutionService(
            CalibrationExecutionRepository(database),
            CalibrationSessionRepository(database),
            HubClient(
                settings.hub_url,
                token=settings.resolved_hub_token(),
                timeout=settings.hub_timeout_seconds,
            ),
        )
    except DatabaseUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Calibration execution data is temporarily unavailable",
        ) from None


def get_calibration_review_service(request: Request) -> CalibrationReviewService:
    settings = request.app.state.settings
    try:
        database = get_app_database(
            request.app, path=settings.database_path, hermes_home=settings.hermes_home
        )
        return CalibrationReviewService(
            CalibrationReviewRepository(database),
            CalibrationExecutionRepository(database),
            CalibrationSessionRepository(database),
            HubClient(
                settings.hub_url,
                token=settings.resolved_hub_token(),
                timeout=max(settings.hub_timeout_seconds, 30),
            ),
            public_base_url=settings.public_base_url,
            signing_key=settings.gateway_token,
        )
    except DatabaseUnavailableError:
        raise HTTPException(status_code=503, detail="Calibration review data is temporarily unavailable") from None


@router.post(
    "/api/calibrations",
    response_model=CalibrationSessionSummary,
    status_code=status.HTTP_201_CREATED,
    operation_id="createCalibrationSession",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={
        200: {"model": CalibrationSessionSummary, "description": "Idempotent replay"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Agent is not registered"},
        503: {"model": ErrorResponse, "description": "Calibration data unavailable"},
    },
)
def create_calibration_session(
    payload: CreateCalibrationSessionRequest,
    request: Request,
    response: Response,
    service: Annotated[CalibrationService, Depends(get_calibration_service)],
) -> CalibrationSessionSummary:
    if payload.agent_id in AGENT_IDS:
        request.state.agent_id = payload.agent_id
    try:
        session, created = service.create_session(payload)
    except AgentNotRegisteredError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        ) from None
    except (AgentProfileUnavailableError, CalibrationServiceUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Calibration data is temporarily unavailable",
        ) from None

    request.state.session_id = session.session_id
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return session


@router.get(
    "/api/calibrations/{session_id}",
    response_model=CalibrationSessionResponse,
    operation_id="getCalibrationSession",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Session not found"},
        503: {"model": ErrorResponse, "description": "Calibration data unavailable"},
    },
)
def get_calibration_session(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    service: Annotated[CalibrationService, Depends(get_calibration_service)],
) -> CalibrationSessionResponse:
    try:
        session = service.get_session(session_id)
    except CalibrationSessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Calibration session not found",
        ) from None
    except CalibrationServiceUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Calibration data is temporarily unavailable",
        ) from None

    request.state.session_id = session.session_id
    request.state.agent_id = session.agent_id
    return session


@router.delete(
    "/api/calibrations/{session_id}",
    response_model=CalibrationSessionSummary,
    include_in_schema=False,
    dependencies=[Depends(require_bearer_token)],
)
def archive_calibration_session(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    service: Annotated[CalibrationService, Depends(get_calibration_service)],
) -> CalibrationSessionSummary:
    try:
        session = service.archive_session(session_id)
    except CalibrationSessionNotFoundError:
        raise HTTPException(
            status_code=404, detail="Calibration session not found"
        ) from None
    except CalibrationSessionNotWritableError:
        raise HTTPException(
            status_code=409,
            detail="Cannot archive a session while calibration work is active",
        ) from None
    except CalibrationServiceUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Calibration data is temporarily unavailable",
        ) from None
    request.state.session_id = session_id
    request.state.agent_id = session.agent_id
    return session


@router.post(
    "/api/calibrations/{session_id}/restore",
    response_model=CalibrationSessionSummary,
    include_in_schema=False,
    dependencies=[Depends(require_bearer_token)],
)
def restore_calibration_session(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    service: Annotated[CalibrationService, Depends(get_calibration_service)],
) -> CalibrationSessionSummary:
    try:
        session = service.restore_session(session_id)
    except CalibrationSessionNotFoundError:
        raise HTTPException(
            status_code=404, detail="Calibration session not found"
        ) from None
    except CalibrationServiceUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Calibration data is temporarily unavailable",
        ) from None
    request.state.session_id = session_id
    request.state.agent_id = session.agent_id
    return session


@router.post(
    "/api/calibrations/{session_id}/turns",
    response_model=CalibrationTurnResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="recordCalibrationTurn",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={
        200: {"model": CalibrationTurnResponse, "description": "Idempotent replay"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Session not found"},
        409: {"model": ErrorResponse, "description": "Session is not writable"},
        503: {"model": ErrorResponse, "description": "Calibration data unavailable"},
    },
)
def record_calibration_turn(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: RecordCalibrationTurnRequest,
    request: Request,
    response: Response,
    service: Annotated[CalibrationService, Depends(get_calibration_service)],
) -> CalibrationTurnResponse:
    try:
        turn, created = service.record_turn(session_id, payload)
    except CalibrationSessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Calibration session not found",
        ) from None
    except CalibrationSessionNotWritableError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Calibration session is not writable",
        ) from None
    except CalibrationTurnConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Turn idempotency key conflicts with another session",
        ) from None
    except CalibrationServiceUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Calibration data is temporarily unavailable",
        ) from None

    request.state.session_id = turn.session_id
    request.state.turn_id = turn.turn_id
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return turn


@router.post(
    "/api/calibrations/{session_id}/executions",
    response_model=CalibrationExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="executeCalibrationTurn",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={
        200: {"model": CalibrationExecutionResponse, "description": "Idempotent replay"},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def execute_calibration_turn(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: ExecuteCalibrationTurnRequest,
    request: Request,
    response: Response,
    service: Annotated[
        CalibrationExecutionService, Depends(get_calibration_execution_service)
    ],
) -> CalibrationExecutionResponse:
    try:
        execution, created = service.execute(session_id, payload)
    except (CalibrationSessionNotFoundError, CalibrationExecutionNotFoundError):
        raise HTTPException(status_code=404, detail="Calibration session not found") from None
    except CalibrationExecutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except CalibrationExecutionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Calibration execution is temporarily unavailable") from None
    request.state.session_id = session_id
    request.state.turn_id = execution.execution_id
    request.state.agent_id = execution.agent_id
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return execution


@router.get(
    "/api/calibrations/{session_id}/executions/{execution_id}",
    response_model=CalibrationExecutionResponse,
    operation_id="getCalibrationTurn",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_calibration_turn(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    execution_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    service: Annotated[
        CalibrationExecutionService, Depends(get_calibration_execution_service)
    ],
) -> CalibrationExecutionResponse:
    try:
        execution = service.get(session_id, execution_id)
    except CalibrationExecutionNotFoundError:
        raise HTTPException(status_code=404, detail="Calibration execution not found") from None
    except CalibrationExecutionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Calibration execution is temporarily unavailable") from None
    request.state.session_id = session_id
    request.state.turn_id = execution_id
    request.state.agent_id = execution.agent_id
    return execution


@router.get(
    "/api/calibrations/{session_id}/executions",
    response_model=CalibrationExecutionListResponse,
    include_in_schema=False,
    dependencies=[Depends(require_bearer_token)],
)
def list_calibration_executions(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[CalibrationExecutionService, Depends(get_calibration_execution_service)],
) -> CalibrationExecutionListResponse:
    try:
        return service.list_for_session(session_id)
    except CalibrationExecutionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Calibration execution data is temporarily unavailable") from None


@router.post(
    "/api/calibrations/{session_id}/executions/{execution_id}/reviews",
    response_model=CalibrationReviewResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="createCalibrationReview",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={200: {"model": CalibrationReviewResponse}, 401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def create_calibration_review(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    execution_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: CreateCalibrationReviewRequest,
    request: Request,
    response: Response,
    service: Annotated[CalibrationReviewService, Depends(get_calibration_review_service)],
) -> CalibrationReviewResponse:
    try:
        review, created = service.create(session_id, execution_id, payload)
    except (CalibrationSessionNotFoundError, CalibrationExecutionNotFoundError, CalibrationReviewNotFoundError):
        raise HTTPException(status_code=404, detail="Creator calibration execution not found") from None
    except CalibrationReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except CalibrationReviewServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Calibration review is temporarily unavailable") from None
    request.state.session_id = session_id
    request.state.turn_id = review.review_id
    request.state.agent_id = "critic"
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return review


@router.get(
    "/api/calibrations/{session_id}/executions/{execution_id}/reviews/{review_id}",
    response_model=CalibrationReviewResponse,
    operation_id="getCalibrationReview",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_calibration_review(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    execution_id: Annotated[str, Path(min_length=1, max_length=128)],
    review_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    service: Annotated[CalibrationReviewService, Depends(get_calibration_review_service)],
) -> CalibrationReviewResponse:
    try:
        review = service.get(session_id, review_id)
        if review.execution_id != execution_id:
            raise CalibrationReviewNotFoundError(review_id)
    except CalibrationReviewNotFoundError:
        raise HTTPException(status_code=404, detail="Calibration review not found") from None
    except CalibrationReviewServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Calibration review is temporarily unavailable") from None
    request.state.session_id = session_id
    request.state.turn_id = review_id
    return review


@router.get(
    "/api/calibrations/{session_id}/reviews/{review_id}/evidence",
    response_model=CalibrationEvidenceResponse,
    operation_id="getCalibrationEvidence",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_calibration_evidence(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    review_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[CalibrationReviewService, Depends(get_calibration_review_service)],
) -> CalibrationEvidenceResponse:
    try:
        return service.evidence(session_id, review_id)
    except CalibrationReviewNotFoundError:
        raise HTTPException(status_code=404, detail="Calibration review not found") from None
    except CalibrationReviewServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Calibration evidence is temporarily unavailable") from None


@router.get(
    "/api/calibrations/{session_id}/reviews",
    response_model=CalibrationReviewListResponse,
    include_in_schema=False,
    dependencies=[Depends(require_bearer_token)],
)
def list_calibration_reviews(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[CalibrationReviewService, Depends(get_calibration_review_service)],
) -> CalibrationReviewListResponse:
    try:
        return CalibrationReviewListResponse(session_id=session_id, reviews=service.list_for_session(session_id))
    except CalibrationReviewServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Calibration reviews are temporarily unavailable") from None


@router.get(
    "/api/calibrations/{session_id}/assets/{asset_id}/link",
    include_in_schema=False,
    dependencies=[Depends(require_bearer_token)],
)
def get_calibration_asset_link(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    asset_id: Annotated[int, Path(gt=0)],
    service: Annotated[CalibrationReviewService, Depends(get_calibration_review_service)],
) -> dict[str, object]:
    if not service.review_repository.asset_belongs_to_session(session_id, asset_id):
        raise HTTPException(status_code=404, detail="Calibration asset not found")
    try:
        summary = service.hub_client.request("GET", f"/api/assets/{asset_id}/summary").get("asset", {})
    except HubUnavailableError:
        summary = {}
    return {
        "asset_id": asset_id,
        "asset_type": str(summary.get("asset_type") or "other"),
        "mime_type": str(summary.get("mime_type") or "application/octet-stream"),
        "title": str(summary.get("title") or f"Asset #{asset_id}"),
        "url": service.signed_asset_url(session_id, asset_id),
    }


@router.get("/api/calibration-evidence/{asset_id}", include_in_schema=False)
def get_signed_calibration_asset(
    asset_id: Annotated[int, Path(gt=0)],
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    expires: Annotated[int, Query(gt=0)],
    sig: Annotated[str, Query(min_length=64, max_length=64)],
    service: Annotated[CalibrationReviewService, Depends(get_calibration_review_service)],
) -> Response:
    if not service.verify_asset_signature(session_id, asset_id, expires, sig):
        raise HTTPException(status_code=403, detail="Evidence link is invalid or expired")
    try:
        body, content_type, filename = service.hub_client.request_bytes(f"/api/assets/{asset_id}/file")
    except HubNotFoundError:
        raise HTTPException(status_code=404, detail="Evidence asset not found") from None
    except HubUnavailableError:
        raise HTTPException(status_code=503, detail="Evidence asset is temporarily unavailable") from None
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{filename.replace(chr(34), "")}"', "Cache-Control": "private, max-age=300"},
    )


@router.get(
    "/api/agents/{agent_id}/calibrations",
    response_model=AgentCalibrationListResponse,
    operation_id="listAgentCalibrations",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Agent is not registered"},
        503: {"model": ErrorResponse, "description": "Calibration data unavailable"},
    },
)
def list_agent_calibrations(
    agent_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=32,
            json_schema_extra={"enum": AGENT_IDS},
        ),
    ],
    request: Request,
    service: Annotated[CalibrationService, Depends(get_calibration_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AgentCalibrationListResponse:
    try:
        result = service.list_agent_sessions(agent_id, limit)
    except AgentNotRegisteredError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        ) from None
    except (AgentProfileUnavailableError, CalibrationServiceUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Calibration data is temporarily unavailable",
        ) from None

    request.state.agent_id = result.agent_id
    return result
