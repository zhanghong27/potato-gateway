from __future__ import annotations

from pydantic import ValidationError

from potato_gateway.models import (
    AgentCalibrationListResponse,
    CalibrationSessionResponse,
    CalibrationSessionSummary,
    CalibrationTurnResponse,
    CreateCalibrationSessionRequest,
    RecordCalibrationTurnRequest,
)
from potato_gateway.repositories import (
    CalibrationPersistenceError,
    CalibrationSessionNotFoundError,
    CalibrationSessionRecord,
    CalibrationSessionRepository,
    CalibrationTurnRecord,
)
from potato_gateway.services.agent_profile_service import AgentProfileService


class CalibrationServiceUnavailableError(Exception):
    pass


class CalibrationService:
    def __init__(
        self,
        session_repository: CalibrationSessionRepository,
        agent_profile_service: AgentProfileService,
    ) -> None:
        self.session_repository = session_repository
        self.agent_profile_service = agent_profile_service

    def create_session(
        self, request: CreateCalibrationSessionRequest
    ) -> tuple[CalibrationSessionSummary, bool]:
        try:
            existing = self.session_repository.get_session_by_client_request_id(
                request.client_request_id
            )
            if existing is not None:
                return self._session_summary(existing), False
        except (CalibrationPersistenceError, ValidationError):
            raise CalibrationServiceUnavailableError from None

        profile = self.agent_profile_service.get_profile(request.agent_id)
        try:
            record, created = self.session_repository.create_session(
                client_request_id=request.client_request_id,
                agent_id=request.agent_id,
                goal=request.goal,
                acceptance_criteria=request.acceptance_criteria,
                transport=request.transport,
                base_prompt_version=profile.prompt.version,
                base_prompt_content_sha256=profile.prompt.content_sha256,
            )
            return self._session_summary(record), created
        except (CalibrationPersistenceError, ValidationError):
            raise CalibrationServiceUnavailableError from None

    def get_session(self, session_id: str) -> CalibrationSessionResponse:
        try:
            record = self.session_repository.get_session(session_id)
            if record is None:
                raise CalibrationSessionNotFoundError(session_id)
            turns = self.session_repository.list_turns(session_id)
            return CalibrationSessionResponse(
                **self._session_summary(record).model_dump(),
                turns=[self._turn_response(turn) for turn in turns],
            )
        except CalibrationSessionNotFoundError:
            raise
        except (CalibrationPersistenceError, ValidationError):
            raise CalibrationServiceUnavailableError from None

    def record_turn(
        self, session_id: str, request: RecordCalibrationTurnRequest
    ) -> tuple[CalibrationTurnResponse, bool]:
        try:
            record, created = self.session_repository.create_turn(
                session_id=session_id,
                client_turn_id=request.client_turn_id,
                actor=request.actor,
                kind=request.kind,
                content=request.content,
            )
            return self._turn_response(record), created
        except (CalibrationPersistenceError, ValidationError):
            raise CalibrationServiceUnavailableError from None

    def list_agent_sessions(
        self, agent_id: str, limit: int
    ) -> AgentCalibrationListResponse:
        self.agent_profile_service.get_profile(agent_id)
        try:
            records = self.session_repository.list_sessions(agent_id, limit)
            return AgentCalibrationListResponse(
                agent_id=agent_id,
                sessions=[self._session_summary(record) for record in records],
            )
        except (CalibrationPersistenceError, ValidationError):
            raise CalibrationServiceUnavailableError from None

    @staticmethod
    def _session_summary(
        record: CalibrationSessionRecord,
    ) -> CalibrationSessionSummary:
        return CalibrationSessionSummary(
            session_id=record.session_id,
            client_request_id=record.client_request_id,
            agent_id=record.agent_id,
            state=record.state,
            transport=record.transport,
            goal=record.goal,
            acceptance_criteria=record.acceptance_criteria,
            base_prompt_version=record.base_prompt_version,
            base_prompt_content_sha256=record.base_prompt_content_sha256,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _turn_response(record: CalibrationTurnRecord) -> CalibrationTurnResponse:
        return CalibrationTurnResponse(
            turn_id=record.turn_id,
            session_id=record.session_id,
            client_turn_id=record.client_turn_id,
            actor=record.actor,
            kind=record.kind,
            content=record.content,
            created_at=record.created_at,
        )
