from __future__ import annotations

from datetime import datetime, timezone

from potato_gateway.adapters import (
    AgentRegistration,
    HermesProfileAdapter,
    HermesProfileSourceError,
)
from potato_gateway.config import Settings
from potato_gateway.database import Database
from potato_gateway.models import AgentIdentity, AgentProfileResponse
from potato_gateway.repositories import (
    CalibrationSessionRepository,
    CalibrationStateRepository,
    CalibrationStateSourceError,
)


class AgentProfileUnavailableError(Exception):
    pass


class AgentProfileService:
    def __init__(
        self,
        profile_adapter: HermesProfileAdapter,
        calibration_repository: CalibrationStateRepository,
    ) -> None:
        self.profile_adapter = profile_adapter
        self.calibration_repository = calibration_repository

    def get_profile(self, agent_id: str) -> AgentProfileResponse:
        try:
            registration = self.profile_adapter.get_registration(agent_id)
            profile, prompt = self.profile_adapter.read_profile(registration)
            calibration = self.calibration_repository.get(agent_id)
        except (HermesProfileSourceError, CalibrationStateSourceError):
            raise AgentProfileUnavailableError(agent_id) from None

        return AgentProfileResponse(
            agent=self._agent_identity(agent_id, registration),
            profile=profile,
            prompt=prompt,
            calibration=calibration,
            observed_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _agent_identity(
        agent_id: str, registration: AgentRegistration
    ) -> AgentIdentity:
        return AgentIdentity(
            id=agent_id,
            display_name=registration.display_name,
            role=registration.role,
        )


def build_agent_profile_service(
    settings: Settings, database: Database
) -> AgentProfileService:
    session_repository = CalibrationSessionRepository(database)
    return AgentProfileService(
        profile_adapter=HermesProfileAdapter(
            hermes_home=settings.hermes_home,
            registry_path=settings.agent_registry_path,
        ),
        calibration_repository=CalibrationStateRepository(
            state_dir=settings.calibration_state_dir,
            hermes_home=settings.hermes_home,
            session_repository=session_repository,
        ),
    )
