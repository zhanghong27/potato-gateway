from __future__ import annotations

import hashlib
import hmac
import time
from pydantic import ValidationError

from urllib.parse import quote
from urllib.parse import urlencode

from potato_gateway.adapters import (
    HubClient,
    HubNotFoundError,
    HubUnavailableError,
)
from potato_gateway.models import (
    CalibrationAssetSourceListResponse,
    CalibrationAssetSourceResponse,
    CalibrationSubmissionAsset,
    CalibrationSubmissionListResponse,
    CalibrationSubmissionResponse,
    CreateCalibrationSubmissionRequest,
)
from potato_gateway.repositories import (
    CalibrationExecutionPersistenceError,
    CalibrationExecutionRepository,
    CalibrationSubmissionConflictError,
    CalibrationSubmissionNotFoundError,
    CalibrationSubmissionPersistenceError,
    CalibrationSubmissionRecord,
    CalibrationSubmissionRepository,
)


class CalibrationSubmissionServiceUnavailableError(Exception):
    pass


class CalibrationSubmissionService:
    def __init__(
        self,
        repository: CalibrationSubmissionRepository,
        execution_repository: CalibrationExecutionRepository,
        hub_client: HubClient,
        *,
        public_base_url: str,
        signing_key: str,
    ) -> None:
        self.repository = repository
        self.execution_repository = execution_repository
        self.hub_client = hub_client
        self.public_base_url = public_base_url.rstrip("/")
        self.signing_key = signing_key.encode("utf-8")

    def signed_source_asset_url(
        self, source_id: str, asset_id: int, ttl_seconds: int = 900
    ) -> str:
        source = self.get_source(source_id)
        asset = next((item for item in source.assets if item.id == asset_id), None)
        if not asset or not asset.available:
            raise CalibrationSubmissionNotFoundError(str(asset_id))
        expires = int(time.time()) + ttl_seconds
        signature = self._source_signature(source_id, asset_id, expires)
        query = urlencode(
            {
                "source_id": source_id,
                "expires": expires,
                "sig": signature,
            }
        )
        return f"{self.public_base_url}/api/calibration-source-assets/{asset_id}?{query}"

    def source_asset_preview(
        self, source_id: str, asset_id: int
    ) -> CalibrationSubmissionAsset:
        source = self.get_source(source_id)
        source_asset = next(
            (item for item in source.assets if item.id == asset_id), None
        )
        if not source_asset or not source_asset.available:
            raise CalibrationSubmissionNotFoundError(str(asset_id))
        return self._asset_preview(asset_id)

    def verify_source_asset_signature(
        self, source_id: str, asset_id: int, expires: int, signature: str
    ) -> bool:
        if expires < int(time.time()) or expires > int(time.time()) + 1800:
            return False
        expected = self._source_signature(source_id, asset_id, expires)
        if not hmac.compare_digest(expected, signature):
            return False
        try:
            source = self.get_source(source_id)
        except (
            CalibrationSubmissionNotFoundError,
            CalibrationSubmissionServiceUnavailableError,
        ):
            return False
        return any(item.id == asset_id and item.available for item in source.assets)

    def _source_signature(self, source_id: str, asset_id: int, expires: int) -> str:
        message = f"{source_id}:{asset_id}:{expires}".encode("utf-8")
        return hmac.new(self.signing_key, message, hashlib.sha256).hexdigest()

    def list_sources(self) -> CalibrationAssetSourceListResponse:
        try:
            payload = self.hub_client.request(
                "GET", "/api/calibration-asset-sources"
            )
            return CalibrationAssetSourceListResponse(
                sources=payload.get("sources", [])
            )
        except (HubUnavailableError, ValidationError, TypeError, ValueError):
            raise CalibrationSubmissionServiceUnavailableError from None

    def get_source(self, source_id: str) -> CalibrationAssetSourceResponse:
        try:
            payload = self.hub_client.request(
                "GET", f"/api/calibration-asset-sources/{quote(source_id, safe='')}"
            )
            return CalibrationAssetSourceResponse.model_validate(payload["source"])
        except (HubNotFoundError, KeyError):
            raise CalibrationSubmissionNotFoundError(source_id) from None
        except (HubUnavailableError, ValidationError, TypeError, ValueError):
            raise CalibrationSubmissionServiceUnavailableError from None

    def create_existing(
        self, session_id: str, request: CreateCalibrationSubmissionRequest
    ) -> tuple[CalibrationSubmissionResponse, bool]:
        try:
            source = self.get_source(request.source_id)
            source_asset_ids = {asset.id for asset in source.assets if asset.available}
            requested_asset_ids = {
                request.primary_video_asset_id,
                *(item.asset_id for item in request.support_assets),
            }
            if not requested_asset_ids.issubset(source_asset_ids):
                raise CalibrationSubmissionConflictError(
                    "all submission assets must belong to the selected source"
                )
            primary = self._asset_preview(request.primary_video_asset_id)
            if primary.asset_type != "video" or not primary.available:
                raise CalibrationSubmissionConflictError(
                    "primary asset must be an available video"
                )
            support_assets: list[dict[str, object]] = []
            seen = {request.primary_video_asset_id}
            for item in request.support_assets:
                if item.asset_id in seen:
                    raise CalibrationSubmissionConflictError(
                        "an asset can only appear once in a submission"
                    )
                asset = self._asset_preview(item.asset_id, role=item.role)
                if not asset.available:
                    raise CalibrationSubmissionConflictError(
                        "support assets must be available"
                    )
                seen.add(item.asset_id)
                support_assets.append(
                    {"asset_id": item.asset_id, "role": item.role}
                )
            record, created = self.repository.create_existing(
                client_request_id=request.client_request_id,
                session_id=session_id,
                primary_video_asset_id=request.primary_video_asset_id,
                support_assets=support_assets,
                source_id=request.source_id,
                parent_submission_id=request.parent_submission_id or "",
            )
            return self._response(record), created
        except (
            CalibrationSubmissionNotFoundError,
            CalibrationSubmissionConflictError,
        ):
            raise
        except (
            CalibrationSubmissionPersistenceError,
            HubUnavailableError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            raise CalibrationSubmissionServiceUnavailableError from None

    def get(
        self, session_id: str, submission_id: str
    ) -> CalibrationSubmissionResponse:
        try:
            record = self.repository.get(submission_id)
            if record.session_id != session_id:
                raise CalibrationSubmissionNotFoundError(submission_id)
            return self._response(record)
        except CalibrationSubmissionNotFoundError:
            raise
        except (
            CalibrationSubmissionPersistenceError,
            HubUnavailableError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            raise CalibrationSubmissionServiceUnavailableError from None

    def list_for_session(
        self, session_id: str
    ) -> CalibrationSubmissionListResponse:
        try:
            self._sync_live_executions(session_id)
            return CalibrationSubmissionListResponse(
                session_id=session_id,
                submissions=[
                    self._response(record)
                    for record in self.repository.list_for_session(session_id)
                ],
            )
        except (
            CalibrationSubmissionPersistenceError,
            CalibrationExecutionPersistenceError,
            HubUnavailableError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            raise CalibrationSubmissionServiceUnavailableError from None

    def _sync_live_executions(self, session_id: str) -> None:
        for execution in self.execution_repository.list_for_session(session_id):
            if execution.status != "completed" or not execution.asset_ids:
                continue
            assets = [self._asset_preview(asset_id) for asset_id in execution.asset_ids]
            videos = [asset for asset in assets if asset.asset_type == "video" and asset.available]
            if not videos:
                continue
            primary = videos[0]
            support = [
                {"asset_id": asset.asset_id, "role": asset.role}
                for asset in assets
                if asset.asset_id != primary.asset_id and asset.available
            ]
            self.repository.ensure_live(
                session_id=session_id,
                execution_id=execution.execution_id,
                primary_video_asset_id=primary.asset_id,
                support_assets=support,
            )

    def _asset_preview(
        self, asset_id: int, role: str | None = None
    ) -> CalibrationSubmissionAsset:
        payload = self.hub_client.request(
            "GET", f"/api/assets/{asset_id}/calibration-preview"
        )
        preview = payload["preview"]
        asset = preview["asset"]
        return CalibrationSubmissionAsset(
            asset_id=int(asset["id"]),
            role=role or str(asset.get("suggested_role") or "other"),
            asset_type=str(asset.get("asset_type") or "other"),
            title=str(asset.get("title") or f"Asset #{asset_id}"),
            mime_type=str(asset.get("mime_type") or "application/octet-stream"),
            available=bool(asset.get("available")),
            text_preview=str(preview.get("text_preview") or ""),
            preview_truncated=bool(preview.get("truncated")),
        )

    def _response(
        self, record: CalibrationSubmissionRecord
    ) -> CalibrationSubmissionResponse:
        primary = self._asset_preview(record.primary_video_asset_id, role="other")
        support = [
            self._asset_preview(
                int(item["asset_id"]), role=str(item.get("role") or "other")
            )
            for item in record.support_assets
        ]
        return CalibrationSubmissionResponse(
            submission_id=record.submission_id,
            session_id=record.session_id,
            source_type=record.source_type,
            execution_id=record.execution_id or None,
            primary_video=primary,
            support_assets=support,
            source_id=record.source_id or None,
            parent_submission_id=record.parent_submission_id or None,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
