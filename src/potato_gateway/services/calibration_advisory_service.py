from __future__ import annotations

from pydantic import ValidationError

from potato_gateway.adapters import HubClient, HubConflictError, HubUnavailableError
from potato_gateway.models import (
    CalibrationAdvisoryBundle,
    CalibrationHistoryRound,
    CalibrationAdvisoryListResponse,
    CalibrationAdvisoryResponse,
    CreateCalibrationAdvisoryRequest,
    SubmitCalibrationAdvisoryRequest,
)
from potato_gateway.repositories import (
    CalibrationAdvisoryConflictError,
    CalibrationAdvisoryNotFoundError,
    CalibrationAdvisoryPersistenceError,
    CalibrationAdvisoryRecord,
    CalibrationAdvisoryRepository,
    CalibrationReviewNotFoundError,
    CalibrationReviewPersistenceError,
    CalibrationReviewRepository,
    CalibrationSessionNotFoundError,
    CalibrationSessionRepository,
    CalibrationSubmissionNotFoundError,
    CalibrationSubmissionPersistenceError,
    CalibrationSubmissionRepository,
    PromptVersionConflictError,
    PromptVersionNotFoundError,
)
from potato_gateway.services.calibration_review_service import (
    CalibrationReviewService,
    CalibrationReviewServiceUnavailableError,
)
from potato_gateway.services.calibration_submission_service import (
    CalibrationSubmissionService,
    CalibrationSubmissionServiceUnavailableError,
)
from potato_gateway.services.prompt_version_service import (
    PromptVersionService,
    PromptVersionServiceUnavailableError,
)


class CalibrationAdvisoryServiceUnavailableError(Exception):
    pass


class CalibrationAdvisoryService:
    def __init__(
        self,
        repository: CalibrationAdvisoryRepository,
        session_repository: CalibrationSessionRepository,
        submission_repository: CalibrationSubmissionRepository,
        review_repository: CalibrationReviewRepository,
        submission_service: CalibrationSubmissionService,
        review_service: CalibrationReviewService,
        prompt_service: PromptVersionService,
        hub_client: HubClient,
    ) -> None:
        self.repository = repository
        self.session_repository = session_repository
        self.submission_repository = submission_repository
        self.review_repository = review_repository
        self.submission_service = submission_service
        self.review_service = review_service
        self.prompt_service = prompt_service
        self.hub_client = hub_client

    def create(
        self, session_id: str, request: CreateCalibrationAdvisoryRequest
    ) -> tuple[CalibrationAdvisoryResponse, bool]:
        try:
            session = self.session_repository.get_session(session_id)
            if session is None:
                raise CalibrationSessionNotFoundError(session_id)
            submission = self.submission_repository.get(request.submission_id)
            if submission.session_id != session_id:
                raise CalibrationSubmissionNotFoundError(request.submission_id)
            review = self.review_repository.get(request.review_id)
            if (
                review.session_id != session_id
                or review.submission_id != request.submission_id
            ):
                raise CalibrationReviewNotFoundError(request.review_id)
            if review.status != "completed":
                raise CalibrationAdvisoryConflictError(
                    "ChatGPT analysis requires a completed critic review"
                )
            pending = self.repository.find_pending(
                session_id=session_id,
                submission_id=request.submission_id,
                review_id=request.review_id,
            )
            if pending is not None:
                return self._response(pending), False
            record, created = self.repository.create(
                client_request_id=request.client_request_id,
                session_id=session_id,
                submission_id=request.submission_id,
                review_id=request.review_id,
            )
            return self._response(record), created
        except (
            CalibrationAdvisoryConflictError,
            CalibrationReviewNotFoundError,
            CalibrationSessionNotFoundError,
            CalibrationSubmissionNotFoundError,
        ):
            raise
        except (
            CalibrationAdvisoryPersistenceError,
            CalibrationReviewPersistenceError,
            CalibrationSubmissionPersistenceError,
        ):
            raise CalibrationAdvisoryServiceUnavailableError from None

    def get(self, advisory_id: str) -> CalibrationAdvisoryResponse:
        try:
            return self._response(self.repository.get(advisory_id))
        except CalibrationAdvisoryNotFoundError:
            raise
        except (CalibrationAdvisoryPersistenceError, ValidationError):
            raise CalibrationAdvisoryServiceUnavailableError from None

    def list(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> CalibrationAdvisoryListResponse:
        try:
            return CalibrationAdvisoryListResponse(
                advisories=[
                    self._response(record)
                    for record in self.repository.list(
                        status=status, session_id=session_id, limit=limit
                    )
                ]
            )
        except (CalibrationAdvisoryPersistenceError, ValidationError):
            raise CalibrationAdvisoryServiceUnavailableError from None

    def get_for_prompt_version(
        self, prompt_version_id: str
    ) -> CalibrationAdvisoryResponse | None:
        try:
            record = self.repository.find_by_prompt_version(prompt_version_id)
            return self._response(record) if record is not None else None
        except (CalibrationAdvisoryPersistenceError, ValidationError):
            raise CalibrationAdvisoryServiceUnavailableError from None

    def bundle(self, advisory_id: str) -> CalibrationAdvisoryBundle:
        try:
            record = self.repository.get(advisory_id)
            session = self.session_repository.get_session(record.session_id)
            if session is None:
                raise CalibrationSessionNotFoundError(record.session_id)
            submission = self.submission_service.get(
                record.session_id, record.submission_id
            )
            review = self.review_service.get(record.session_id, record.review_id)
            evidence = self.review_service.evidence(
                record.session_id, record.review_id
            )
            feedback = [
                turn.content
                for turn in self.session_repository.list_turns(record.session_id)
                if turn.actor == "user"
                and turn.kind in {"critique", "note"}
                and turn.content.strip()
            ][-10:]
            active = self.prompt_service.active_version(session.agent_id)
            history: list[CalibrationHistoryRound] = []
            for prior in self.repository.list(
                status="completed", session_id=record.session_id, limit=8
            ):
                if prior.advisory_id == record.advisory_id or not prior.analysis:
                    continue
                prior_analysis = SubmitCalibrationAdvisoryRequest.model_validate(
                    prior.analysis
                )
                prior_review = self.review_repository.get(prior.review_id)
                report = (
                    prior_review.report
                    if isinstance(prior_review.report, dict)
                    else {}
                )
                score = report.get("total_score")
                history.append(
                    CalibrationHistoryRound(
                        advisory_id=prior.advisory_id,
                        submission_id=prior.submission_id,
                        review_id=prior.review_id,
                        review_verdict=str(report.get("verdict") or "unknown"),
                        review_score=(
                            float(score)
                            if isinstance(score, (int, float))
                            and not isinstance(score, bool)
                            else None
                        ),
                        review_summary=str(report.get("summary") or ""),
                        advisory_summary=prior_analysis.executive_summary,
                        capability_patch=prior_analysis.capability_patch,
                        tooling_task_titles=[
                            task.title for task in prior_analysis.tooling_tasks
                        ],
                        created_at=prior.created_at,
                    )
                )
            return CalibrationAdvisoryBundle(
                advisory=self._response(record),
                agent_id=session.agent_id,
                goal=session.goal,
                acceptance_criteria=session.acceptance_criteria,
                user_feedback=feedback,
                submission=submission,
                critic_review=review,
                evidence=evidence,
                calibration_history=history,
                active_prompt_content_sha256=active.content_sha256,
            )
        except (
            CalibrationAdvisoryNotFoundError,
            CalibrationSessionNotFoundError,
            CalibrationSubmissionNotFoundError,
            CalibrationReviewNotFoundError,
        ):
            raise
        except (
            CalibrationAdvisoryPersistenceError,
            CalibrationSubmissionServiceUnavailableError,
            CalibrationReviewServiceUnavailableError,
            PromptVersionServiceUnavailableError,
            ValidationError,
        ):
            raise CalibrationAdvisoryServiceUnavailableError from None

    def submit(
        self, advisory_id: str, analysis: SubmitCalibrationAdvisoryRequest
    ) -> CalibrationAdvisoryResponse:
        try:
            record = self.repository.get(advisory_id)
            if record.status == "completed":
                stored = SubmitCalibrationAdvisoryRequest.model_validate(
                    record.analysis
                )
                if stored != analysis:
                    raise CalibrationAdvisoryConflictError(
                        "completed advisory analysis is immutable"
                    )
                return self._response(record)
            bundle = self.bundle(advisory_id)
            allowed_assets = {
                bundle.submission.primary_video.asset_id,
                *(item.asset_id for item in bundle.submission.support_assets),
                *(item.asset_id for item in bundle.evidence.frames),
                *(item.asset_id for item in bundle.evidence.contact_sheets),
            }
            cited_assets = {
                asset_id
                for finding in analysis.findings
                for asset_id in finding.evidence_asset_ids
            } | {
                asset_id
                for action in analysis.priority_actions
                for asset_id in action.evidence_asset_ids
            } | {
                asset_id
                for task in analysis.tooling_tasks
                for asset_id in task.evidence_asset_ids
            }
            if not cited_assets.issubset(allowed_assets):
                raise CalibrationAdvisoryConflictError(
                    "analysis cites assets outside the advisory bundle"
                )
            self._dispatch_tooling_tasks(record, analysis)
            candidate, _created = self.prompt_service.create_advised_candidate(
                agent_id=bundle.agent_id,
                session_id=record.session_id,
                advisory_id=record.advisory_id,
                analysis=analysis,
            )
            completed = self.repository.complete(
                advisory_id,
                analysis=analysis.model_dump(mode="json"),
                prompt_version_id=candidate.prompt_version_id,
            )
            return self._response(completed)
        except HubConflictError as exc:
            raise CalibrationAdvisoryConflictError(str(exc)) from None
        except (
            CalibrationAdvisoryConflictError,
            CalibrationAdvisoryNotFoundError,
            CalibrationSessionNotFoundError,
            CalibrationSubmissionNotFoundError,
            CalibrationReviewNotFoundError,
            PromptVersionConflictError,
            PromptVersionNotFoundError,
        ):
            raise
        except (
            CalibrationAdvisoryPersistenceError,
            CalibrationSubmissionServiceUnavailableError,
            CalibrationReviewServiceUnavailableError,
            PromptVersionServiceUnavailableError,
            HubUnavailableError,
            ValidationError,
        ):
            raise CalibrationAdvisoryServiceUnavailableError from None

    def _dispatch_tooling_tasks(
        self,
        record: CalibrationAdvisoryRecord,
        analysis: SubmitCalibrationAdvisoryRequest,
    ) -> None:
        for index, task in enumerate(analysis.tooling_tasks, start=1):
            self.hub_client.request(
                "POST",
                "/api/calibration-tooling-tasks",
                {
                    "client_request_id": f"tooling.{record.advisory_id}.{index}",
                    "session_id": record.session_id,
                    "advisory_id": record.advisory_id,
                    "task_index": index,
                    **task.model_dump(mode="json"),
                },
                sanitize=False,
            )

    @staticmethod
    def _response(record: CalibrationAdvisoryRecord) -> CalibrationAdvisoryResponse:
        analysis = (
            SubmitCalibrationAdvisoryRequest.model_validate(record.analysis)
            if record.analysis
            else None
        )
        return CalibrationAdvisoryResponse(
            advisory_id=record.advisory_id,
            session_id=record.session_id,
            submission_id=record.submission_id,
            review_id=record.review_id,
            status=record.status,
            analysis=analysis,
            prompt_version_id=record.prompt_version_id or None,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at or None,
        )
