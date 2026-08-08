from __future__ import annotations

import os
import tempfile
from pathlib import Path

from potato_gateway.adapters import HermesProfileAdapter, HermesProfileSourceError
from potato_gateway.models import (
    CreatePromptCandidateRequest,
    PromptVersionListResponse,
    PromptVersionSummary,
)
from potato_gateway.repositories import (
    CalibrationReviewPersistenceError,
    CalibrationReviewRepository,
    PromptVersionConflictError,
    PromptVersionNotFoundError,
    PromptVersionPersistenceError,
    PromptVersionRecord,
    PromptVersionRepository,
)


class PromptVersionServiceUnavailableError(Exception):
    pass


class PromptVersionService:
    def __init__(
        self,
        repository: PromptVersionRepository,
        profile_adapter: HermesProfileAdapter,
        review_repository: CalibrationReviewRepository | None = None,
    ) -> None:
        self.repository = repository
        self.profile_adapter = profile_adapter
        self.review_repository = review_repository

    def create_candidate(
        self, agent_id: str, request: CreatePromptCandidateRequest
    ) -> tuple[PromptVersionSummary, bool]:
        try:
            active = self._ensure_snapshot(agent_id)
            record, created = self.repository.create_candidate(
                client_request_id=request.client_request_id,
                agent_id=agent_id,
                content=request.content,
                base_content_sha256=active.base_content_sha256,
                change_summary=request.change_summary,
                calibration_session_id=request.calibration_session_id or "",
            )
            return self._summary(record), created
        except (PromptVersionConflictError, PromptVersionNotFoundError):
            raise
        except (PromptVersionPersistenceError, HermesProfileSourceError, OSError):
            raise PromptVersionServiceUnavailableError from None

    def list_versions(self, agent_id: str, limit: int) -> PromptVersionListResponse:
        try:
            self._ensure_snapshot(agent_id)
            return PromptVersionListResponse(
                agent_id=agent_id,
                versions=[self._summary(item) for item in self.repository.list(agent_id, limit)],
            )
        except (PromptVersionPersistenceError, HermesProfileSourceError, OSError):
            raise PromptVersionServiceUnavailableError from None

    def promote(
        self, agent_id: str, prompt_version_id: str, confirm_content_sha256: str
    ) -> PromptVersionSummary:
        try:
            candidate = self.repository.get(prompt_version_id)
            if candidate.agent_id != agent_id:
                raise PromptVersionNotFoundError(prompt_version_id)
            if candidate.content_sha256 != confirm_content_sha256:
                raise PromptVersionConflictError("confirmation hash does not match")
            if (
                agent_id == "creator"
                and candidate.calibration_session_id
                and self.review_repository is not None
                and self.review_repository.session_has_hard_errors(candidate.calibration_session_id)
            ):
                raise PromptVersionConflictError("unresolved critic hard errors block creator prompt activation")
            prompt_path, original = self.profile_adapter.read_primary_prompt(agent_id)
            self._atomic_write(prompt_path, candidate.content)
            try:
                activated = self.repository.activate(prompt_version_id)
            except Exception:
                self._atomic_write(prompt_path, original)
                raise
            return self._summary(activated)
        except (PromptVersionConflictError, PromptVersionNotFoundError):
            raise
        except (PromptVersionPersistenceError, CalibrationReviewPersistenceError, HermesProfileSourceError, OSError):
            raise PromptVersionServiceUnavailableError from None

    def mark_testing(self, agent_id: str, prompt_version_id: str) -> PromptVersionSummary:
        try:
            candidate = self.repository.get(prompt_version_id)
            if candidate.agent_id != agent_id:
                raise PromptVersionNotFoundError(prompt_version_id)
            return self._summary(self.repository.mark_testing(prompt_version_id))
        except (PromptVersionConflictError, PromptVersionNotFoundError):
            raise
        except PromptVersionPersistenceError:
            raise PromptVersionServiceUnavailableError from None

    def _ensure_snapshot(self, agent_id: str) -> PromptVersionRecord:
        registration = self.profile_adapter.get_registration(agent_id)
        profile, prompt = self.profile_adapter.read_profile(registration)
        _path, content = self.profile_adapter.read_primary_prompt(agent_id)
        return self.repository.ensure_active_snapshot(
            agent_id=agent_id,
            content=content,
            profile_content_sha256=prompt.content_sha256,
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path = path.resolve()
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _summary(record: PromptVersionRecord) -> PromptVersionSummary:
        return PromptVersionSummary(
            prompt_version_id=record.prompt_version_id,
            agent_id=record.agent_id,
            status=record.status,
            content_sha256=record.content_sha256,
            base_content_sha256=record.base_content_sha256,
            change_summary=record.change_summary,
            calibration_session_id=record.calibration_session_id or None,
            created_at=record.created_at,
            updated_at=record.updated_at,
            activated_at=record.activated_at or None,
        )
