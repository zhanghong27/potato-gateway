from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from potato_gateway.models import CalibrationInfo, CalibrationState, LatestEvaluation
from potato_gateway.repositories.calibration_session_repository import (
    CalibrationPersistenceError,
    CalibrationSessionRepository,
)


LOGGER = logging.getLogger("potato_gateway.calibration")
MAX_STATE_BYTES = 256 * 1024
UNTRACKED_MESSAGE = "No structured calibration record exists yet"


class CalibrationStateSourceError(Exception):
    pass


class CalibrationStateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    agent_id: str
    state: CalibrationState
    latest_session_id: str | None = None
    last_activity_at: datetime | None = None
    current_prompt_version: str | None = None
    candidate_prompt_version: str | None = None
    latest_evaluation: LatestEvaluation | None = None
    message: str


class CalibrationStateRepository:
    def __init__(
        self,
        state_dir: Path,
        hermes_home: Path,
        session_repository: CalibrationSessionRepository | None = None,
    ) -> None:
        self.hermes_home = hermes_home.expanduser().resolve()
        self.state_dir = self._resolve_configured_path(state_dir)
        self.session_repository = session_repository

    def get(self, agent_id: str) -> CalibrationInfo:
        if self.session_repository is not None:
            try:
                open_session = self.session_repository.latest_open_session(agent_id)
                if open_session is not None:
                    message = (
                        f"{open_session.transport.title()} calibration session is active"
                        if open_session.state == "calibrating"
                        else f"{open_session.transport.title()} calibration session is blocked"
                    )
                    return CalibrationInfo(
                        state=open_session.state,
                        latest_session_id=open_session.session_id,
                        last_activity_at=open_session.updated_at,
                        current_prompt_version=open_session.base_prompt_version,
                        candidate_prompt_version=None,
                        latest_evaluation=None,
                        message=message,
                    )
                if self.session_repository.has_sessions(agent_id):
                    return self._untracked()
            except CalibrationPersistenceError:
                LOGGER.error(
                    "SQLite calibration state is unavailable for agent_id=%s; "
                    "using legacy fallback",
                    agent_id,
                )

        return self._get_legacy(agent_id)

    def _get_legacy(self, agent_id: str) -> CalibrationInfo:
        state_path = (self.state_dir / f"{agent_id}.json").resolve()
        if not self._is_within(state_path, self.state_dir):
            self._raise_invalid(agent_id)
        if not state_path.exists():
            return self._untracked()

        try:
            if not state_path.is_file() or state_path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("invalid state file")
            document = CalibrationStateDocument.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
            if document.agent_id != agent_id:
                raise ValueError("agent mismatch")
            return CalibrationInfo(
                state=document.state,
                latest_session_id=document.latest_session_id,
                last_activity_at=document.last_activity_at,
                current_prompt_version=document.current_prompt_version,
                candidate_prompt_version=document.candidate_prompt_version,
                latest_evaluation=document.latest_evaluation,
                message=document.message,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError):
            self._raise_invalid(agent_id)

    @staticmethod
    def _untracked() -> CalibrationInfo:
        return CalibrationInfo(
            state="untracked",
            latest_session_id=None,
            last_activity_at=None,
            current_prompt_version=None,
            candidate_prompt_version=None,
            latest_evaluation=None,
            message=UNTRACKED_MESSAGE,
        )

    def _resolve_configured_path(self, path: Path) -> Path:
        expanded = path.expanduser()
        candidate = expanded if expanded.is_absolute() else Path.cwd() / expanded
        resolved = candidate.resolve()
        if not self._is_within(resolved, self.hermes_home):
            raise CalibrationStateSourceError("calibration source is unavailable")
        return resolved

    def _raise_invalid(self, agent_id: str) -> None:
        LOGGER.error(
            "Calibration state is invalid or unavailable for agent_id=%s", agent_id
        )
        raise CalibrationStateSourceError("calibration state is unavailable") from None

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True
