from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status

from potato_gateway.adapters import (
    HubClient,
    HubConflictError,
    HubNotFoundError,
    HubUnavailableError,
)
from potato_gateway.auth import require_bearer_token
from potato_gateway.models import (
    ApprovalDecisionRequest,
    CreateVideoWorkflowRequest,
    ErrorResponse,
    WorkflowMessageRequest,
)


AGENT_NAMES = {
    "researcher": "薯博士",
    "creator": "清蒸土豆",
    "critic": "酸辣土豆丝",
    "engineer": "薯码宝贝",
    "all": "all",
    "user": "user",
}
router = APIRouter()


def get_hub_client(request: Request) -> HubClient:
    settings = request.app.state.settings
    return HubClient(
        settings.hub_url,
        token=settings.resolved_hub_token(),
        timeout=settings.hub_timeout_seconds,
    )


def _hub_call(call):
    try:
        return call()
    except HubNotFoundError:
        raise HTTPException(status_code=404, detail="Potato Hub object not found") from None
    except HubConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except HubUnavailableError:
        raise HTTPException(status_code=503, detail="Potato Hub is temporarily unavailable") from None


@router.post(
    "/api/workflows/video",
    operation_id="createVideoWorkflow",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_bearer_token)],
    tags=["workflows"],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def create_video_workflow(
    payload: CreateVideoWorkflowRequest,
    request: Request,
    response: Response,
    hub: Annotated[HubClient, Depends(get_hub_client)],
) -> dict[str, Any]:
    request.state.session_id = payload.client_request_id
    result = _hub_call(
        lambda: hub.request(
            "POST",
            "/api/workflows/video",
            {
                "title": payload.title,
                "request": payload.request,
                "requirements": payload.requirements,
                "asset_ids": payload.asset_ids,
                "max_revisions": payload.max_revisions,
                "handoff_policy": payload.handoff_policy.model_dump(),
                "requested_by": "土豆总指挥",
            },
            headers={"X-Idempotency-Key": payload.client_request_id},
        )
    )
    response.status_code = status.HTTP_201_CREATED if result.get("created", True) else status.HTTP_200_OK
    return result


@router.get(
    "/api/workflows/{workflow_id}",
    operation_id="getWorkflow",
    dependencies=[Depends(require_bearer_token)],
    tags=["workflows"],
)
def get_workflow(
    workflow_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    hub: Annotated[HubClient, Depends(get_hub_client)],
) -> dict[str, Any]:
    request.state.session_id = workflow_id
    return _hub_call(lambda: hub.request("GET", f"/api/workflows/{workflow_id}"))


@router.get(
    "/api/workflows/{workflow_id}/events",
    operation_id="listWorkflowEvents",
    dependencies=[Depends(require_bearer_token)],
    tags=["workflows"],
)
def list_workflow_events(
    workflow_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    hub: Annotated[HubClient, Depends(get_hub_client)],
) -> dict[str, Any]:
    request.state.session_id = workflow_id
    return _hub_call(lambda: hub.request("GET", f"/api/workflows/{workflow_id}/events?limit=500"))


@router.post(
    "/api/workflows/{workflow_id}/messages",
    operation_id="sendWorkflowMessage",
    dependencies=[Depends(require_bearer_token)],
    tags=["workflows"],
)
def send_workflow_message(
    workflow_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: WorkflowMessageRequest,
    request: Request,
    hub: Annotated[HubClient, Depends(get_hub_client)],
) -> dict[str, Any]:
    request.state.session_id = workflow_id
    return _hub_call(
        lambda: hub.request(
            "POST",
            f"/api/workflows/{workflow_id}/messages",
            {
                "sender_name": "土豆总指挥",
                "sender_type": "user",
                "target": AGENT_NAMES[payload.target],
                "content": payload.content,
                "message_type": "chat",
            },
        )
    )


@router.post(
    "/api/approvals/{approval_id}/decision",
    operation_id="decideWorkflowApproval",
    dependencies=[Depends(require_bearer_token)],
    tags=["workflows"],
)
def decide_workflow_approval(
    approval_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: ApprovalDecisionRequest,
    hub: Annotated[HubClient, Depends(get_hub_client)],
) -> dict[str, Any]:
    return _hub_call(
        lambda: hub.request(
            "POST",
            f"/api/approvals/{approval_id}/decision",
            {"decision": payload.decision, "note": payload.note, "decided_by": "土豆总指挥"},
        )
    )


@router.get(
    "/api/assets/{asset_id}/summary",
    operation_id="getAssetSummary",
    dependencies=[Depends(require_bearer_token)],
    tags=["assets"],
)
def get_asset_summary(
    asset_id: Annotated[int, Path(ge=1)],
    hub: Annotated[HubClient, Depends(get_hub_client)],
) -> dict[str, Any]:
    return _hub_call(lambda: hub.request("GET", f"/api/assets/{asset_id}/summary"))


@router.get(
    "/api/reviews/{review_id}",
    operation_id="getVideoReview",
    dependencies=[Depends(require_bearer_token)],
    tags=["reviews"],
)
def get_video_review(
    review_id: Annotated[str, Path(min_length=1, max_length=128)],
    hub: Annotated[HubClient, Depends(get_hub_client)],
) -> dict[str, Any]:
    return _hub_call(lambda: hub.request("GET", f"/api/reviews/{review_id}"))
