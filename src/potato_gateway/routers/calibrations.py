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

from potato_gateway.adapters import AgentNotRegisteredError, HermesProfileAdapter, HermesProfileSourceError, HubClient, HubNotFoundError, HubUnavailableError
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
    CalibrationSubmissionAsset,
    CalibrationAssetSourceListResponse,
    CalibrationAssetSourceResponse,
    CalibrationSubmissionListResponse,
    CalibrationSubmissionResponse,
    CalibrationTurnResponse,
    CreateCalibrationSessionRequest,
    CreateCalibrationReviewRequest,
    CreateCalibrationSubmissionRequest,
    CreateCalibrationSubmissionReviewRequest,
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
    CalibrationSubmissionConflictError,
    CalibrationSubmissionNotFoundError,
    CalibrationSubmissionRepository,
    PromptVersionConflictError,
    PromptVersionNotFoundError,
    PromptVersionRepository,
)
from potato_gateway.services import (
    AgentProfileUnavailableError,
    CalibrationService,
    CalibrationServiceUnavailableError,
    CalibrationExecutionService,
    CalibrationExecutionServiceUnavailableError,
    CalibrationReviewService,
    CalibrationReviewServiceUnavailableError,
    CalibrationSubmissionService,
    CalibrationSubmissionServiceUnavailableError,
    PromptVersionService,
    PromptVersionServiceUnavailableError,
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


def get_calibration_prompt_service(request: Request) -> PromptVersionService:
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
            CalibrationSessionRepository(database),
        )
    except (DatabaseUnavailableError, HermesProfileSourceError):
        raise HTTPException(
            status_code=503,
            detail="Prompt candidate data is temporarily unavailable",
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
            CalibrationSubmissionRepository(database),
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


def get_calibration_submission_service(
    request: Request,
) -> CalibrationSubmissionService:
    settings = request.app.state.settings
    try:
        database = get_app_database(
            request.app,
            path=settings.database_path,
            hermes_home=settings.hermes_home,
        )
        return CalibrationSubmissionService(
            CalibrationSubmissionRepository(database),
            CalibrationExecutionRepository(database),
            HubClient(
                settings.hub_url,
                token=settings.resolved_hub_token(),
                timeout=max(settings.hub_timeout_seconds, 30),
            ),
            public_base_url=settings.public_base_url,
            signing_key=settings.gateway_token,
        )
    except DatabaseUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Calibration submission data is temporarily unavailable",
        ) from None


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


@router.get(
    "/api/calibration-asset-sources",
    response_model=CalibrationAssetSourceListResponse,
    operation_id="listCalibrationAssetSources",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
)
def list_calibration_asset_sources(
    service: Annotated[
        CalibrationSubmissionService,
        Depends(get_calibration_submission_service),
    ],
) -> CalibrationAssetSourceListResponse:
    try:
        return service.list_sources()
    except CalibrationSubmissionServiceUnavailableError:
        raise HTTPException(
            status_code=503, detail="Calibration assets are temporarily unavailable"
        ) from None


@router.get(
    "/api/calibration-asset-sources/{source_id}",
    response_model=CalibrationAssetSourceResponse,
    operation_id="getCalibrationAssetSource",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
)
def get_calibration_asset_source(
    source_id: Annotated[str, Path(min_length=1, max_length=160)],
    service: Annotated[
        CalibrationSubmissionService,
        Depends(get_calibration_submission_service),
    ],
) -> CalibrationAssetSourceResponse:
    try:
        return service.get_source(source_id)
    except CalibrationSubmissionNotFoundError:
        raise HTTPException(status_code=404, detail="Asset source not found") from None
    except CalibrationSubmissionServiceUnavailableError:
        raise HTTPException(
            status_code=503, detail="Calibration assets are temporarily unavailable"
        ) from None


@router.get(
    "/api/calibration-asset-sources/{source_id}/assets/{asset_id}/link",
    include_in_schema=False,
    dependencies=[Depends(require_bearer_token)],
)
def get_calibration_source_asset_link(
    source_id: Annotated[str, Path(min_length=1, max_length=160)],
    asset_id: Annotated[int, Path(gt=0)],
    service: Annotated[
        CalibrationSubmissionService,
        Depends(get_calibration_submission_service),
    ],
) -> dict[str, object]:
    try:
        return {
            "asset_id": asset_id,
            "url": service.signed_source_asset_url(source_id, asset_id),
        }
    except CalibrationSubmissionNotFoundError:
        raise HTTPException(status_code=404, detail="Source asset not found") from None
    except CalibrationSubmissionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Source asset unavailable") from None


@router.get(
    "/api/calibration-asset-sources/{source_id}/assets/{asset_id}/preview",
    response_model=CalibrationSubmissionAsset,
    include_in_schema=False,
    dependencies=[Depends(require_bearer_token)],
)
def get_calibration_source_asset_preview(
    source_id: Annotated[str, Path(min_length=1, max_length=160)],
    asset_id: Annotated[int, Path(gt=0)],
    service: Annotated[
        CalibrationSubmissionService,
        Depends(get_calibration_submission_service),
    ],
) -> CalibrationSubmissionAsset:
    try:
        return service.source_asset_preview(source_id, asset_id)
    except CalibrationSubmissionNotFoundError:
        raise HTTPException(status_code=404, detail="Source asset not found") from None
    except CalibrationSubmissionServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Source asset unavailable") from None


@router.get("/api/calibration-source-assets/{asset_id}", include_in_schema=False)
def get_signed_calibration_source_asset(
    asset_id: Annotated[int, Path(gt=0)],
    source_id: Annotated[str, Query(min_length=1, max_length=160)],
    expires: Annotated[int, Query(gt=0)],
    sig: Annotated[str, Query(min_length=64, max_length=64)],
    service: Annotated[
        CalibrationSubmissionService,
        Depends(get_calibration_submission_service),
    ],
) -> Response:
    if not service.verify_source_asset_signature(
        source_id, asset_id, expires, sig
    ):
        raise HTTPException(status_code=403, detail="Asset link is invalid or expired")
    try:
        body, content_type, filename = service.hub_client.request_bytes(
            f"/api/assets/{asset_id}/file"
        )
    except HubNotFoundError:
        raise HTTPException(status_code=404, detail="Asset not found") from None
    except HubUnavailableError:
        raise HTTPException(status_code=503, detail="Asset unavailable") from None
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename.replace(chr(34), "")}"',
            "Cache-Control": "private, max-age=300",
        },
    )


@router.post(
    "/api/calibrations/{session_id}/submissions",
    response_model=CalibrationSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createCalibrationSubmission",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={200: {"model": CalibrationSubmissionResponse}},
)
def create_calibration_submission(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: CreateCalibrationSubmissionRequest,
    request: Request,
    response: Response,
    service: Annotated[
        CalibrationSubmissionService,
        Depends(get_calibration_submission_service),
    ],
) -> CalibrationSubmissionResponse:
    try:
        submission, created = service.create_existing(session_id, payload)
    except CalibrationSubmissionNotFoundError:
        raise HTTPException(
            status_code=404, detail="Calibration session or asset source not found"
        ) from None
    except CalibrationSubmissionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except CalibrationSubmissionServiceUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Calibration submission is temporarily unavailable",
        ) from None
    request.state.session_id = session_id
    request.state.turn_id = submission.submission_id
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return submission


@router.get(
    "/api/calibrations/{session_id}/submissions",
    response_model=CalibrationSubmissionListResponse,
    operation_id="listCalibrationSubmissions",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
)
def list_calibration_submissions(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[
        CalibrationSubmissionService,
        Depends(get_calibration_submission_service),
    ],
) -> CalibrationSubmissionListResponse:
    try:
        return service.list_for_session(session_id)
    except CalibrationSubmissionServiceUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Calibration submissions are temporarily unavailable",
        ) from None


@router.get(
    "/api/calibrations/{session_id}/submissions/{submission_id}",
    response_model=CalibrationSubmissionResponse,
    operation_id="getCalibrationSubmission",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
)
def get_calibration_submission(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    submission_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[
        CalibrationSubmissionService,
        Depends(get_calibration_submission_service),
    ],
) -> CalibrationSubmissionResponse:
    try:
        return service.get(session_id, submission_id)
    except CalibrationSubmissionNotFoundError:
        raise HTTPException(status_code=404, detail="Submission not found") from None
    except CalibrationSubmissionServiceUnavailableError:
        raise HTTPException(
            status_code=503, detail="Submission is temporarily unavailable"
        ) from None


@router.post(
    "/api/calibrations/{session_id}/submissions/{submission_id}/reviews",
    response_model=CalibrationReviewResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="createCalibrationSubmissionReview",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
    responses={200: {"model": CalibrationReviewResponse}},
)
def create_calibration_submission_review(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    submission_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: CreateCalibrationSubmissionReviewRequest,
    request: Request,
    response: Response,
    service: Annotated[
        CalibrationReviewService, Depends(get_calibration_review_service)
    ],
) -> CalibrationReviewResponse:
    try:
        review, created = service.create_for_submission(
            session_id, submission_id, payload.client_request_id
        )
    except (CalibrationSubmissionNotFoundError, CalibrationReviewNotFoundError):
        raise HTTPException(status_code=404, detail="Submission not found") from None
    except CalibrationReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except CalibrationReviewServiceUnavailableError:
        raise HTTPException(
            status_code=503, detail="Calibration review is temporarily unavailable"
        ) from None
    request.state.session_id = session_id
    request.state.turn_id = review.review_id
    request.state.agent_id = "critic"
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return review


@router.get(
    "/api/calibrations/{session_id}/submissions/{submission_id}/reviews/{review_id}",
    response_model=CalibrationReviewResponse,
    operation_id="getCalibrationSubmissionReview",
    dependencies=[Depends(require_bearer_token)],
    tags=["calibrations"],
)
def get_calibration_submission_review(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    submission_id: Annotated[str, Path(min_length=1, max_length=128)],
    review_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[
        CalibrationReviewService, Depends(get_calibration_review_service)
    ],
) -> CalibrationReviewResponse:
    try:
        review = service.get(session_id, review_id)
        if review.submission_id != submission_id:
            raise CalibrationReviewNotFoundError(review_id)
        return review
    except CalibrationReviewNotFoundError:
        raise HTTPException(status_code=404, detail="Calibration review not found") from None
    except CalibrationReviewServiceUnavailableError:
        raise HTTPException(
            status_code=503, detail="Calibration review is temporarily unavailable"
        ) from None


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


@router.post(
    "/api/calibrations/{session_id}/prompt-candidates/{prompt_version_id}/tests",
    response_model=CalibrationExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="testPromptCandidate",
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
def test_prompt_candidate(
    session_id: Annotated[str, Path(min_length=1, max_length=128)],
    prompt_version_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: ExecuteCalibrationTurnRequest,
    request: Request,
    response: Response,
    execution_service: Annotated[
        CalibrationExecutionService, Depends(get_calibration_execution_service)
    ],
    prompt_service: Annotated[
        PromptVersionService, Depends(get_calibration_prompt_service)
    ],
) -> CalibrationExecutionResponse:
    try:
        session = (
            prompt_service.session_repository.get_session(session_id)
            if prompt_service.session_repository
            else None
        )
        if session is None:
            raise CalibrationSessionNotFoundError(session_id)
        _candidate, profile_name = prompt_service.prepare_test(
            session.agent_id, session_id, prompt_version_id
        )
        execution, created = execution_service.execute(
            session_id,
            payload,
            prompt_version_id=prompt_version_id,
            profile_override=profile_name,
        )
    except (CalibrationSessionNotFoundError, PromptVersionNotFoundError):
        raise HTTPException(status_code=404, detail="Prompt candidate not found") from None
    except (CalibrationExecutionConflictError, PromptVersionConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (
        CalibrationExecutionServiceUnavailableError,
        PromptVersionServiceUnavailableError,
    ):
        raise HTTPException(status_code=503, detail="Prompt candidate test could not be queued") from None
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
