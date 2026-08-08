from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from pydantic import ValidationError

from potato_gateway.adapters import HubClient, HubUnavailableError
from potato_gateway.models import (
    CalibrationEvidenceFile,
    CalibrationEvidenceFrame,
    CalibrationEvidenceResponse,
    CalibrationReviewResponse,
    CreateCalibrationReviewRequest,
)
from potato_gateway.repositories import (
    CalibrationExecutionNotFoundError,
    CalibrationExecutionRepository,
    CalibrationReviewConflictError,
    CalibrationReviewNotFoundError,
    CalibrationReviewPersistenceError,
    CalibrationReviewRecord,
    CalibrationReviewRepository,
    CalibrationSessionNotFoundError,
    CalibrationSessionRepository,
    CalibrationTurnConflictError,
)


class CalibrationReviewServiceUnavailableError(Exception):
    pass


class CalibrationReviewService:
    def __init__(self, review_repository: CalibrationReviewRepository, execution_repository: CalibrationExecutionRepository, session_repository: CalibrationSessionRepository, hub_client: HubClient, *, public_base_url: str, signing_key: str) -> None:
        self.review_repository = review_repository
        self.execution_repository = execution_repository
        self.session_repository = session_repository
        self.hub_client = hub_client
        self.public_base_url = public_base_url.rstrip("/")
        self.signing_key = signing_key.encode("utf-8")

    def create(self, session_id: str, execution_id: str, request: CreateCalibrationReviewRequest) -> tuple[CalibrationReviewResponse, bool]:
        try:
            session = self.session_repository.get_session(session_id)
            if session is None:
                raise CalibrationSessionNotFoundError(session_id)
            execution = self.execution_repository.get(execution_id)
            if execution.session_id != session_id:
                raise CalibrationExecutionNotFoundError(execution_id)
            record, created = self.review_repository.create(
                client_request_id=request.client_request_id,
                session_id=session_id,
                execution_id=execution_id,
                source_asset_id=request.source_asset_id,
            )
            if created:
                result = self.hub_client.request(
                    "POST",
                    "/api/calibration-review-jobs",
                    {
                        "client_request_id": record.review_id,
                        "session_id": session_id,
                        "source_calibration_job_id": execution.hub_job_id,
                        "source_execution_id": execution_id,
                        "source_asset_id": request.source_asset_id,
                    },
                    sanitize=False,
                )
                record = self.review_repository.sync(
                    record.review_id,
                    hub_review_job_id=str(result["calibration_review_job"]["review_job_id"]),
                )
            return self._response(record), created
        except (CalibrationSessionNotFoundError, CalibrationExecutionNotFoundError, CalibrationReviewNotFoundError, CalibrationReviewConflictError):
            raise
        except (CalibrationReviewPersistenceError, HubUnavailableError, KeyError, TypeError, ValueError, ValidationError):
            raise CalibrationReviewServiceUnavailableError from None

    def get(self, session_id: str, review_id: str) -> CalibrationReviewResponse:
        return self._response(self._sync(session_id, review_id))

    def list_for_session(self, session_id: str) -> list[CalibrationReviewResponse]:
        try:
            return [self._response(self._sync(session_id, item.review_id)) for item in self.review_repository.list_for_session(session_id)]
        except CalibrationReviewNotFoundError:
            return []

    def evidence(self, session_id: str, review_id: str) -> CalibrationEvidenceResponse:
        record = self._sync(session_id, review_id)
        package = record.review_package
        observations = {}
        for item in record.report.get("shot_assessments", []):
            if isinstance(item, dict):
                for asset_id in item.get("evidence_asset_ids", []):
                    observations[int(asset_id)] = str(item.get("description") or "逐镜头校准证据")
        frames = []
        for item in package.get("evidence", []):
            if not isinstance(item, dict) or not isinstance(item.get("asset_id"), int):
                continue
            asset_id = int(item["asset_id"])
            frames.append(
                CalibrationEvidenceFrame(
                    asset_id=asset_id,
                    shot_index=int(item.get("shot_index") or 0),
                    position=str(item.get("position") or ""),
                    timestamp_seconds=float(item.get("timestamp_seconds") or 0),
                    description=observations.get(asset_id, f"镜头 {item.get('shot_index', 0)} {item.get('position', '')} 帧"),
                    url=self.signed_asset_url(session_id, asset_id),
                )
            )
        sheets = [CalibrationEvidenceFile(asset_id=asset_id, url=self.signed_asset_url(session_id, asset_id)) for asset_id in record.contact_sheet_asset_ids]
        return CalibrationEvidenceResponse(
            review_id=review_id, session_id=session_id, status=record.status,
            frames=frames, contact_sheets=sheets,
            transcript_status=str(package.get("transcript_status") or "unavailable"),
            mechanical_metrics=package.get("mechanical_metrics") if isinstance(package.get("mechanical_metrics"), dict) else {},
            openaiFileResponse=[item.url for item in sheets[:4]],
        )

    def signed_asset_url(self, session_id: str, asset_id: int, ttl_seconds: int = 900) -> str:
        expires = int(time.time()) + ttl_seconds
        signature = self._signature(session_id, asset_id, expires)
        return f"{self.public_base_url}/api/calibration-evidence/{asset_id}?{urlencode({'session_id': session_id, 'expires': expires, 'sig': signature})}"

    def verify_asset_signature(self, session_id: str, asset_id: int, expires: int, signature: str) -> bool:
        if expires < int(time.time()) or expires > int(time.time()) + 1800:
            return False
        expected = self._signature(session_id, asset_id, expires)
        return hmac.compare_digest(expected, signature) and self.review_repository.asset_belongs_to_session(session_id, asset_id)

    def _signature(self, session_id: str, asset_id: int, expires: int) -> str:
        message = f"{session_id}:{asset_id}:{expires}".encode("utf-8")
        return hmac.new(self.signing_key, message, hashlib.sha256).hexdigest()

    def _sync(self, session_id: str, review_id: str) -> CalibrationReviewRecord:
        try:
            record = self.review_repository.get(review_id)
            if record.session_id != session_id:
                raise CalibrationReviewNotFoundError(review_id)
            if record.hub_review_job_id and record.status not in {"completed", "failed"}:
                result = self.hub_client.request("GET", f"/api/calibration-review-jobs/{record.hub_review_job_id}")
                job = result["calibration_review_job"]
                status = str(job.get("status") or record.status)
                record = self.review_repository.sync(
                    review_id, status=status,
                    report=job.get("report") if isinstance(job.get("report"), dict) else {},
                    review_package=job.get("review_package") if isinstance(job.get("review_package"), dict) else {},
                    evidence_asset_ids=job.get("evidence_asset_ids") if isinstance(job.get("evidence_asset_ids"), list) else [],
                    contact_sheet_asset_ids=job.get("contact_sheet_asset_ids") if isinstance(job.get("contact_sheet_asset_ids"), list) else [],
                    error=str(job.get("error") or ""), completed_at=str(job.get("completed_at") or ""),
                )
            if record.status == "completed":
                self.session_repository.create_turn(
                    session_id=session_id, client_turn_id=f"{record.review_id}.critic",
                    actor="evaluator", kind="critique", content=str(record.report.get("summary") or "酸辣土豆丝评审已完成"),
                )
            return record
        except (CalibrationReviewNotFoundError, CalibrationTurnConflictError):
            raise
        except (CalibrationReviewPersistenceError, HubUnavailableError, KeyError, TypeError, ValueError):
            raise CalibrationReviewServiceUnavailableError from None

    @staticmethod
    def _response(record: CalibrationReviewRecord) -> CalibrationReviewResponse:
        return CalibrationReviewResponse(
            review_id=record.review_id, session_id=record.session_id, execution_id=record.execution_id,
            source_asset_id=record.source_asset_id, status=record.status, report=record.report,
            evidence_asset_ids=record.evidence_asset_ids, contact_sheet_asset_ids=record.contact_sheet_asset_ids,
            error=record.error or None, created_at=record.created_at, updated_at=record.updated_at,
            completed_at=record.completed_at or None,
        )
