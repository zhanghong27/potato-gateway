from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from potato_gateway.database import Database, DatabaseUnavailableError


class CalibrationAdvisoryPersistenceError(Exception):
    pass


class CalibrationAdvisoryNotFoundError(Exception):
    pass


class CalibrationAdvisoryConflictError(Exception):
    pass


@dataclass(frozen=True)
class CalibrationAdvisoryRecord:
    advisory_id: str
    client_request_id: str
    session_id: str
    submission_id: str
    review_id: str
    status: str
    analysis: dict[str, object]
    prompt_version_id: str
    created_at: str
    updated_at: str
    completed_at: str


class CalibrationAdvisoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        client_request_id: str,
        session_id: str,
        submission_id: str,
        review_id: str,
    ) -> tuple[CalibrationAdvisoryRecord, bool]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM calibration_advisories WHERE client_request_id = ?",
                    (client_request_id,),
                ).fetchone()
                if existing:
                    record = self._from_row(existing)
                    if (
                        record.session_id != session_id
                        or record.submission_id != submission_id
                        or record.review_id != review_id
                    ):
                        raise CalibrationAdvisoryConflictError(client_request_id)
                    connection.commit()
                    return record, False
                advisory_id = f"cala_{uuid.uuid4().hex}"
                now = self.database.utc_now()
                connection.execute(
                    """
                    INSERT INTO calibration_advisories(
                        advisory_id, client_request_id, session_id,
                        submission_id, review_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        advisory_id,
                        client_request_id,
                        session_id,
                        submission_id,
                        review_id,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM calibration_advisories WHERE advisory_id = ?",
                    (advisory_id,),
                ).fetchone()
                connection.commit()
            return self._from_row(row), True
        except CalibrationAdvisoryConflictError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationAdvisoryPersistenceError from None

    def get(self, advisory_id: str) -> CalibrationAdvisoryRecord:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM calibration_advisories WHERE advisory_id = ?",
                    (advisory_id,),
                ).fetchone()
            if not row:
                raise CalibrationAdvisoryNotFoundError(advisory_id)
            return self._from_row(row)
        except CalibrationAdvisoryNotFoundError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationAdvisoryPersistenceError from None

    def find_pending(
        self, *, session_id: str, submission_id: str, review_id: str
    ) -> CalibrationAdvisoryRecord | None:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM calibration_advisories
                    WHERE session_id = ? AND submission_id = ? AND review_id = ?
                      AND status = 'pending'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (session_id, submission_id, review_id),
                ).fetchone()
            return self._from_row(row) if row else None
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationAdvisoryPersistenceError from None

    def list(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[CalibrationAdvisoryRecord]:
        self.database.initialize()
        clauses: list[str] = []
        parameters: list[object] = []
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if session_id:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT * FROM calibration_advisories
                    {where}
                    ORDER BY created_at DESC, advisory_id DESC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
            return [self._from_row(row) for row in rows]
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationAdvisoryPersistenceError from None

    def complete(
        self,
        advisory_id: str,
        *,
        analysis: dict[str, object],
        prompt_version_id: str,
    ) -> CalibrationAdvisoryRecord:
        self.database.initialize()
        serialized = json.dumps(analysis, ensure_ascii=False, sort_keys=True)
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM calibration_advisories WHERE advisory_id = ?",
                    (advisory_id,),
                ).fetchone()
                if not row:
                    raise CalibrationAdvisoryNotFoundError(advisory_id)
                record = self._from_row(row)
                if record.status == "completed":
                    if (
                        record.analysis != analysis
                        or record.prompt_version_id != prompt_version_id
                    ):
                        raise CalibrationAdvisoryConflictError(advisory_id)
                    connection.commit()
                    return record
                if record.status != "pending":
                    raise CalibrationAdvisoryConflictError(
                        "only a pending advisory can be completed"
                    )
                now = self.database.utc_now()
                connection.execute(
                    """
                    UPDATE calibration_advisories
                    SET status = 'completed', analysis_json = ?,
                        prompt_version_id = ?, updated_at = ?, completed_at = ?
                    WHERE advisory_id = ?
                    """,
                    (serialized, prompt_version_id, now, now, advisory_id),
                )
                updated = connection.execute(
                    "SELECT * FROM calibration_advisories WHERE advisory_id = ?",
                    (advisory_id,),
                ).fetchone()
                connection.commit()
            return self._from_row(updated)
        except (
            CalibrationAdvisoryConflictError,
            CalibrationAdvisoryNotFoundError,
        ):
            raise
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationAdvisoryPersistenceError from None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CalibrationAdvisoryRecord:
        analysis = json.loads(row["analysis_json"] or "{}")
        if not isinstance(analysis, dict):
            analysis = {}
        return CalibrationAdvisoryRecord(
            advisory_id=str(row["advisory_id"]),
            client_request_id=str(row["client_request_id"]),
            session_id=str(row["session_id"]),
            submission_id=str(row["submission_id"]),
            review_id=str(row["review_id"]),
            status=str(row["status"]),
            analysis=analysis,
            prompt_version_id=str(row["prompt_version_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=str(row["completed_at"]),
        )
