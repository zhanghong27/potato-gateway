from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ServiceStatus = Literal["running"]
IntegrationStatus = Literal["online", "offline", "unknown"]
AgentStatus = Literal["online", "offline", "busy", "calibrating", "error", "unknown"]
AgentActivityState = Literal[
    "idle", "working", "calibrating", "error", "offline", "unknown"
]
CalibrationState = Literal[
    "untracked",
    "calibrating",
    "evaluating",
    "needs_revision",
    "stable",
    "blocked",
]
CalibrationSessionState = Literal["calibrating", "blocked", "closed"]
CalibrationActor = Literal["user", "commander", "agent", "evaluator", "system"]
CalibrationTurnKind = Literal["instruction", "response", "critique", "note"]
AgentIdField = Annotated[
    str,
    Field(
        min_length=1,
        max_length=32,
        json_schema_extra={"enum": ["researcher", "creator", "critic", "engineer"]},
    ),
]
CalibratableAgentIdField = Annotated[
    str,
    Field(
        min_length=1,
        max_length=32,
        json_schema_extra={"enum": ["researcher", "creator", "critic"]},
    ),
]
AcceptanceCriterion = Annotated[str, Field(min_length=1, max_length=1000)]
LOCAL_PATH_PATTERN = re.compile(
    r"(?:^|\s)(?:/Users/|/home/|/private/|/tmp/|/var/|/etc/|~/|[A-Za-z]:\\)"
    r"|file://|(?:^|[\\/])\.\.(?:[\\/]|$)"
)


def reject_local_paths(value: str) -> str:
    if LOCAL_PATH_PATTERN.search(value):
        raise ValueError("local filesystem paths are not allowed")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(StrictModel):
    status: Literal["ok"]


class ServiceInfo(StrictModel):
    name: str
    status: ServiceStatus
    version: str


class PotatoHubInfo(StrictModel):
    status: IntegrationStatus
    message: str


class AgentInfo(StrictModel):
    id: str
    display_name: str
    status: AgentStatus
    activity_state: AgentActivityState
    activity_label: str


class StatusResponse(StrictModel):
    service: ServiceInfo
    potato_hub: PotatoHubInfo
    agents: list[AgentInfo]


class AgentIdentity(StrictModel):
    id: str
    display_name: str
    role: str


class HermesProfileInfo(StrictModel):
    provider: Literal["hermes"]
    profile_name: str
    load_status: Literal["loaded"]
    model_provider: str | None
    model_name: str | None
    skills: list[str]
    memory_enabled: bool


class PromptVersionInfo(StrictModel):
    version: str
    version_source: Literal["metadata", "content_hash"]
    content_sha256: str
    updated_at: datetime
    source_files: list[str]


class LatestEvaluation(StrictModel):
    evaluation_id: str
    score: float
    threshold: float
    result: Literal["passed", "failed"]


class CalibrationInfo(StrictModel):
    state: CalibrationState
    latest_session_id: str | None
    last_activity_at: datetime | None
    current_prompt_version: str | None
    candidate_prompt_version: str | None
    latest_evaluation: LatestEvaluation | None
    message: str


class AgentProfileResponse(StrictModel):
    agent: AgentIdentity
    profile: HermesProfileInfo
    prompt: PromptVersionInfo
    calibration: CalibrationInfo
    observed_at: datetime


class ErrorResponse(StrictModel):
    detail: str


class CreateCalibrationSessionRequest(StrictModel):
    client_request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    agent_id: CalibratableAgentIdField
    transport: Literal["manual", "hub"] = "manual"
    goal: str = Field(min_length=1, max_length=4000)
    acceptance_criteria: list[AcceptanceCriterion] = Field(max_length=20)

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("goal must contain non-whitespace characters")
        return reject_local_paths(value)

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_acceptance_criteria(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("acceptance criteria must not be blank")
        return [reject_local_paths(value) for value in values]


class RecordCalibrationTurnRequest(StrictModel):
    client_turn_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    actor: CalibrationActor
    kind: CalibrationTurnKind
    content: str = Field(min_length=1, max_length=50_000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must contain non-whitespace characters")
        return reject_local_paths(value)


class CalibrationSessionSummary(StrictModel):
    session_id: str
    client_request_id: str
    agent_id: str
    state: CalibrationSessionState
    transport: Literal["manual", "hub"]
    goal: str
    acceptance_criteria: list[str]
    base_prompt_version: str
    base_prompt_content_sha256: str
    created_at: datetime
    updated_at: datetime


class CalibrationTurnResponse(StrictModel):
    turn_id: str
    session_id: str
    client_turn_id: str
    actor: CalibrationActor
    kind: CalibrationTurnKind
    content: str
    created_at: datetime


class CalibrationSessionResponse(CalibrationSessionSummary):
    turns: list[CalibrationTurnResponse]


class AgentCalibrationListResponse(StrictModel):
    agent_id: str
    sessions: list[CalibrationSessionSummary]


class HandoffPolicy(StrictModel):
    research_to_creation: Literal["auto", "manual"] = "auto"
    creation_to_review: Literal["auto", "manual"] = "auto"
    review_to_revision: Literal["auto", "manual"] = "auto"


class CreateVideoWorkflowRequest(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(default="", max_length=120)
    request: str = Field(min_length=1, max_length=20_000)
    requirements: dict[str, Any] = Field(default_factory=dict)
    asset_ids: list[int] = Field(default_factory=list, max_length=50)
    max_revisions: int = Field(default=2, ge=0, le=5)
    handoff_policy: HandoffPolicy = Field(default_factory=HandoffPolicy)

    @field_validator("request")
    @classmethod
    def validate_request(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request must contain non-whitespace characters")
        return reject_local_paths(value)


class WorkflowMessageRequest(StrictModel):
    target: Literal["all", "user", "researcher", "creator", "critic", "engineer"] = "all"
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must contain non-whitespace characters")
        return reject_local_paths(value)


class ApprovalDecisionRequest(StrictModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=4000)


class ExecuteCalibrationTurnRequest(StrictModel):
    client_turn_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    instruction: str = Field(min_length=1, max_length=50_000)

    @field_validator("instruction")
    @classmethod
    def validate_instruction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instruction must contain non-whitespace characters")
        return reject_local_paths(value)


class TestPromptCandidateRequest(StrictModel):
    client_turn_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    instruction: str = Field(default="", max_length=50_000)

    @field_validator("instruction")
    @classmethod
    def validate_optional_instruction(cls, value: str) -> str:
        return reject_local_paths(value)


class CalibrationExecutionResponse(StrictModel):
    execution_id: str
    session_id: str
    client_turn_id: str
    agent_id: str
    status: Literal["queued", "running", "completed", "failed"]
    instruction: str
    response: str | None
    asset_ids: list[int]
    error: str | None
    hub_job_id: str | None
    prompt_version_id: str | None = None
    created_at: datetime
    updated_at: datetime


class CalibrationExecutionListResponse(StrictModel):
    session_id: str
    executions: list[CalibrationExecutionResponse]


CalibrationAssetRole = Literal[
    "storyboard",
    "sync_timeline",
    "script",
    "evidence_manifest",
    "subtitle",
    "audio",
    "cover",
    "other",
]


class CalibrationAssetSummary(StrictModel):
    id: int
    asset_type: str
    title: str
    mime_type: str
    file_size: int
    width: int
    height: int
    duration_seconds: float
    status: str
    available: bool
    suggested_role: CalibrationAssetRole = "other"
    preview_available: bool = False


class CalibrationAssetSourceSummary(StrictModel):
    source_id: str
    source_type: Literal["session", "workflow"]
    title: str
    updated_at: str
    asset_count: int
    available_asset_count: int
    video_count: int
    recommended_video_asset_id: int | None


class CalibrationAssetSourceListResponse(StrictModel):
    sources: list[CalibrationAssetSourceSummary]


class CalibrationAssetSourceResponse(StrictModel):
    source_id: str
    source_type: Literal["session", "workflow"]
    title: str
    updated_at: str
    recommended_video_asset_id: int | None
    assets: list[CalibrationAssetSummary]


class CalibrationSupportAssetInput(StrictModel):
    asset_id: int = Field(gt=0)
    role: CalibrationAssetRole


class CreateCalibrationSubmissionRequest(StrictModel):
    client_request_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )
    primary_video_asset_id: int = Field(gt=0)
    support_assets: list[CalibrationSupportAssetInput] = Field(
        default_factory=list, max_length=50
    )
    source_id: str = Field(min_length=1, max_length=160)
    parent_submission_id: str | None = Field(default=None, max_length=128)


class CalibrationSubmissionAsset(StrictModel):
    asset_id: int
    role: CalibrationAssetRole
    asset_type: str
    title: str
    mime_type: str
    available: bool
    text_preview: str = ""
    preview_truncated: bool = False


class CalibrationSubmissionResponse(StrictModel):
    submission_id: str
    session_id: str
    source_type: Literal["live_execution", "existing_assets"]
    execution_id: str | None
    primary_video: CalibrationSubmissionAsset
    support_assets: list[CalibrationSubmissionAsset]
    source_id: str | None
    parent_submission_id: str | None
    status: Literal["ready", "reviewing", "completed", "failed"]
    created_at: datetime
    updated_at: datetime


class CalibrationSubmissionListResponse(StrictModel):
    session_id: str
    submissions: list[CalibrationSubmissionResponse]


class CreateCalibrationSubmissionReviewRequest(StrictModel):
    client_request_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )


class CreateCalibrationReviewRequest(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    source_asset_id: int = Field(gt=0)


class CalibrationReviewResponse(StrictModel):
    review_id: str
    session_id: str
    execution_id: str
    submission_id: str | None = None
    source_asset_id: int
    status: Literal["queued", "preparing", "reviewing", "completed", "failed"]
    report: dict[str, Any]
    evidence_asset_ids: list[int]
    contact_sheet_asset_ids: list[int]
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class CalibrationReviewListResponse(StrictModel):
    session_id: str
    reviews: list[CalibrationReviewResponse]


class CalibrationEvidenceFrame(StrictModel):
    asset_id: int
    shot_index: int
    position: str
    timestamp_seconds: float
    description: str
    url: str


class CalibrationEvidenceFile(StrictModel):
    asset_id: int
    url: str


class CalibrationEvidenceResponse(StrictModel):
    review_id: str
    session_id: str
    status: Literal["queued", "preparing", "reviewing", "completed", "failed"]
    frames: list[CalibrationEvidenceFrame]
    contact_sheets: list[CalibrationEvidenceFile]
    transcript_status: str
    mechanical_metrics: dict[str, Any]
    openaiFileResponse: list[str]


class CreatePromptCandidateRequest(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    content: str = Field(min_length=1, max_length=200_000)
    change_summary: str = Field(min_length=1, max_length=4000)
    calibration_session_id: str | None = Field(default=None, max_length=128)


class GeneratePromptCandidateRequest(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    additional_guidance: str = Field(default="", max_length=12_000)

    @field_validator("additional_guidance")
    @classmethod
    def validate_guidance(cls, value: str) -> str:
        return reject_local_paths(value)


class PromptVersionSummary(StrictModel):
    prompt_version_id: str
    agent_id: str
    status: Literal["draft", "testing", "active", "retired"]
    content_sha256: str
    base_content_sha256: str
    change_summary: str
    managed_addendum: str | None = None
    calibration_session_id: str | None
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None


class PromptVersionListResponse(StrictModel):
    agent_id: str
    versions: list[PromptVersionSummary]


class PromptVersionDetail(PromptVersionSummary):
    content: str


class PromotePromptVersionRequest(StrictModel):
    confirm_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
