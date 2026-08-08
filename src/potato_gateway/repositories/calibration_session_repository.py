from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from potato_gateway.database import Database, DatabaseUnavailableError


class CalibrationPersistenceError(Exception):
    pass


class CalibrationSessionNotFoundError(Exception):
    pass


class CalibrationSessionNotWritableError(Exception):
    pass


class CalibrationTurnConflictError(Exception):
    pass


@dataclass(frozen=True)
class CalibrationSessionRecord:
    session_id: str
    client_request_id: str
    agent_id: str
    state: str
    transport: str
    goal: str
    acceptance_criteria: list[str]
    base_prompt_version: str
    base_prompt_content_sha256: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CalibrationTurnRecord:
    turn_id: str
    session_id: str
    client_turn_id: str
    actor: str
    kind: str
    content: str
    created_at: str


class CalibrationSessionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_session(
        self,
        *,
        client_request_id: str,
        agent_id: str,
        goal: str,
        acceptance_criteria: list[str],
        transport: str,
        base_prompt_version: str,
        base_prompt_content_sha256: str,
    ) -> tuple[CalibrationSessionRecord, bool]:
        self._initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM calibration_sessions "
                    "WHERE client_request_id = ?",
                    (client_request_id,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return self._session_from_row(existing), False

                now = self.database.utc_now()
                session_id = f"cal_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO calibration_sessions (
                        session_id,
                        client_request_id,
                        agent_id,
                        state,
                        transport,
                        goal,
                        acceptance_criteria_json,
                        base_prompt_version,
                        base_prompt_content_sha256,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, 'calibrating', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        client_request_id,
                        agent_id,
                        transport,
                        goal,
                        json.dumps(acceptance_criteria, ensure_ascii=False),
                        base_prompt_version,
                        base_prompt_content_sha256,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM calibration_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.commit()
                return self._session_from_row(row), True
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    def get_session(self, session_id: str) -> CalibrationSessionRecord | None:
        self._initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM calibration_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            return self._session_from_row(row) if row is not None else None
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    def get_session_by_client_request_id(
        self, client_request_id: str
    ) -> CalibrationSessionRecord | None:
        self._initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM calibration_sessions "
                    "WHERE client_request_id = ?",
                    (client_request_id,),
                ).fetchone()
            return self._session_from_row(row) if row is not None else None
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    def list_sessions(
        self, agent_id: str, limit: int
    ) -> list[CalibrationSessionRecord]:
        self._initialize()
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM calibration_sessions
                    WHERE agent_id = ?
                    ORDER BY created_at DESC, session_id DESC
                    LIMIT ?
                    """,
                    (agent_id, limit),
                ).fetchall()
            return [self._session_from_row(row) for row in rows]
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    def list_turns(self, session_id: str) -> list[CalibrationTurnRecord]:
        self._initialize()
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM calibration_turns
                    WHERE session_id = ?
                    ORDER BY created_at ASC, turn_id ASC
                    """,
                    (session_id,),
                ).fetchall()
            return [self._turn_from_row(row) for row in rows]
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    def create_turn(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        actor: str,
        kind: str,
        content: str,
    ) -> tuple[CalibrationTurnRecord, bool]:
        self._initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM calibration_turns WHERE client_turn_id = ?",
                    (client_turn_id,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    turn = self._turn_from_row(existing)
                    if turn.session_id != session_id:
                        raise CalibrationTurnConflictError(client_turn_id)
                    return turn, False

                session = connection.execute(
                    "SELECT state FROM calibration_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise CalibrationSessionNotFoundError(session_id)
                if session["state"] != "calibrating":
                    raise CalibrationSessionNotWritableError(session_id)

                now = self.database.utc_now()
                turn_id = f"turn_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO calibration_turns (
                        turn_id,
                        session_id,
                        client_turn_id,
                        actor,
                        kind,
                        content,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (turn_id, session_id, client_turn_id, actor, kind, content, now),
                )
                connection.execute(
                    "UPDATE calibration_sessions SET updated_at = ? "
                    "WHERE session_id = ?",
                    (now, session_id),
                )
                row = connection.execute(
                    "SELECT * FROM calibration_turns WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                connection.commit()
                return self._turn_from_row(row), True
        except (
            CalibrationSessionNotFoundError,
            CalibrationSessionNotWritableError,
            CalibrationTurnConflictError,
        ):
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    def latest_open_session(
        self, agent_id: str
    ) -> CalibrationSessionRecord | None:
        self._initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM calibration_sessions
                    WHERE agent_id = ? AND state IN ('calibrating', 'blocked')
                    ORDER BY created_at DESC, session_id DESC
                    LIMIT 1
                    """,
                    (agent_id,),
                ).fetchone()
            return self._session_from_row(row) if row is not None else None
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    def has_sessions(self, agent_id: str) -> bool:
        self._initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT 1 FROM calibration_sessions WHERE agent_id = ? LIMIT 1",
                    (agent_id,),
                ).fetchone()
            return row is not None
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    def _initialize(self) -> None:
        try:
            self.database.initialize()
        except DatabaseUnavailableError:
            raise CalibrationPersistenceError("calibration data is unavailable") from None

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> CalibrationSessionRecord:
        criteria = json.loads(row["acceptance_criteria_json"])
        if not isinstance(criteria, list) or not all(
            isinstance(item, str) for item in criteria
        ):
            raise ValueError("invalid acceptance criteria")
        return CalibrationSessionRecord(
            session_id=row["session_id"],
            client_request_id=row["client_request_id"],
            agent_id=row["agent_id"],
            state=row["state"],
            transport=row["transport"],
            goal=row["goal"],
            acceptance_criteria=criteria,
            base_prompt_version=row["base_prompt_version"],
            base_prompt_content_sha256=row["base_prompt_content_sha256"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> CalibrationTurnRecord:
        return CalibrationTurnRecord(
            turn_id=row["turn_id"],
            session_id=row["session_id"],
            client_turn_id=row["client_turn_id"],
            actor=row["actor"],
            kind=row["kind"],
            content=row["content"],
            created_at=row["created_at"],
        )
