from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from potato_gateway.database import Database, DatabaseUnavailableError


class CalibrationExecutionPersistenceError(Exception):
    pass


class CalibrationExecutionNotFoundError(Exception):
    pass


class CalibrationExecutionConflictError(Exception):
    pass


@dataclass(frozen=True)
class CalibrationExecutionRecord:
    execution_id: str
    session_id: str
    client_turn_id: str
    agent_id: str
    status: str
    instruction: str
    response: str
    asset_ids: list[int]
    error: str
    hub_job_id: str
    created_at: str
    updated_at: str


class CalibrationExecutionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        agent_id: str,
        instruction: str,
    ) -> tuple[CalibrationExecutionRecord, bool]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM calibration_executions WHERE client_turn_id = ?",
                    (client_turn_id,),
                ).fetchone()
                if existing:
                    record = self._from_row(existing)
                    if record.session_id != session_id:
                        raise CalibrationExecutionConflictError(client_turn_id)
                    connection.commit()
                    return record, False
                session = connection.execute(
                    "SELECT agent_id, state, transport FROM calibration_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise CalibrationExecutionNotFoundError(session_id)
                if session["state"] != "calibrating" or session["transport"] != "hub":
                    raise CalibrationExecutionConflictError("session is not an active Hub calibration")
                if session["agent_id"] != agent_id:
                    raise CalibrationExecutionConflictError("agent mismatch")
                now = self.database.utc_now()
                execution_id = f"exec_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO calibration_executions(
                        execution_id, session_id, client_turn_id, agent_id,
                        status, instruction, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (execution_id, session_id, client_turn_id, agent_id, instruction, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM calibration_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                connection.commit()
            return self._from_row(row), True
        except (CalibrationExecutionNotFoundError, CalibrationExecutionConflictError):
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationExecutionPersistenceError from None

    def get(self, execution_id: str) -> CalibrationExecutionRecord:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM calibration_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
            if not row:
                raise CalibrationExecutionNotFoundError(execution_id)
            return self._from_row(row)
        except CalibrationExecutionNotFoundError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationExecutionPersistenceError from None

    def list_for_session(self, session_id: str) -> list[CalibrationExecutionRecord]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM calibration_executions WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            return [self._from_row(row) for row in rows]
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationExecutionPersistenceError from None

    def attach_hub_job(self, execution_id: str, hub_job_id: str) -> CalibrationExecutionRecord:
        return self._update(execution_id, status="queued", hub_job_id=hub_job_id)

    def sync(
        self,
        execution_id: str,
        *,
        status: str,
        response: str = "",
        asset_ids: list[int] | None = None,
        error: str = "",
    ) -> CalibrationExecutionRecord:
        if status not in {"queued", "running", "completed", "failed"}:
            raise CalibrationExecutionConflictError("invalid execution status")
        return self._update(
            execution_id,
            status=status,
            response=response,
            asset_ids=asset_ids or [],
            error=error,
        )

    def _update(self, execution_id: str, **values: object) -> CalibrationExecutionRecord:
        self.database.initialize()
        allowed = {"status", "response", "asset_ids", "error", "hub_job_id"}
        if not set(values) <= allowed:
            raise CalibrationExecutionConflictError("invalid execution update")
        assignments: list[str] = []
        params: list[object] = []
        for key, value in values.items():
            column = "asset_ids_json" if key == "asset_ids" else key
            assignments.append(f"{column} = ?")
            params.append(json.dumps(value, ensure_ascii=False) if key == "asset_ids" else value)
        assignments.append("updated_at = ?")
        params.append(self.database.utc_now())
        params.append(execution_id)
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    f"UPDATE calibration_executions SET {', '.join(assignments)} WHERE execution_id = ?",
                    params,
                )
                if cursor.rowcount == 0:
                    raise CalibrationExecutionNotFoundError(execution_id)
                row = connection.execute(
                    "SELECT * FROM calibration_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                connection.commit()
            return self._from_row(row)
        except CalibrationExecutionNotFoundError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationExecutionPersistenceError from None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CalibrationExecutionRecord:
        asset_ids = json.loads(row["asset_ids_json"] or "[]")
        if not isinstance(asset_ids, list) or not all(isinstance(item, int) for item in asset_ids):
            raise CalibrationExecutionPersistenceError
        return CalibrationExecutionRecord(
            execution_id=row["execution_id"],
            session_id=row["session_id"],
            client_turn_id=row["client_turn_id"],
            agent_id=row["agent_id"],
            status=row["status"],
            instruction=row["instruction"],
            response=row["response"],
            asset_ids=asset_ids,
            error=row["error"],
            hub_job_id=row["hub_job_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
