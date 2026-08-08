from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from potato_gateway.database import Database, DatabaseUnavailableError


class CalibrationReviewPersistenceError(Exception):
    pass


class CalibrationReviewNotFoundError(Exception):
    pass


class CalibrationReviewConflictError(Exception):
    pass


@dataclass(frozen=True)
class CalibrationReviewRecord:
    review_id: str
    client_request_id: str
    session_id: str
    execution_id: str
    source_asset_id: int
    hub_review_job_id: str
    status: str
    report: dict
    review_package: dict
    evidence_asset_ids: list[int]
    contact_sheet_asset_ids: list[int]
    error: str
    created_at: str
    updated_at: str
    completed_at: str


class CalibrationReviewRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, *, client_request_id: str, session_id: str, execution_id: str, source_asset_id: int) -> tuple[CalibrationReviewRecord, bool]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM calibration_reviews WHERE client_request_id = ?", (client_request_id,)
                ).fetchone()
                if existing:
                    record = self._from_row(existing)
                    if record.session_id != session_id or record.execution_id != execution_id:
                        raise CalibrationReviewConflictError("review idempotency key conflicts with another execution")
                    connection.commit()
                    return record, False
                execution = connection.execute(
                    "SELECT * FROM calibration_executions WHERE execution_id = ? AND session_id = ?", (execution_id, session_id)
                ).fetchone()
                session = connection.execute(
                    "SELECT agent_id FROM calibration_sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if not execution or not session:
                    raise CalibrationReviewNotFoundError(execution_id)
                assets = json.loads(execution["asset_ids_json"] or "[]")
                if session["agent_id"] != "creator" or execution["status"] != "completed":
                    raise CalibrationReviewConflictError("only a completed creator execution can be reviewed")
                if source_asset_id not in assets:
                    raise CalibrationReviewConflictError("source asset is not attached to this execution")
                now = self.database.utc_now()
                review_id = f"calrev_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO calibration_reviews(
                        review_id, client_request_id, session_id, execution_id,
                        source_asset_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (review_id, client_request_id, session_id, execution_id, source_asset_id, now, now),
                )
                row = connection.execute("SELECT * FROM calibration_reviews WHERE review_id = ?", (review_id,)).fetchone()
                connection.commit()
            return self._from_row(row), True
        except (CalibrationReviewNotFoundError, CalibrationReviewConflictError):
            raise
        except (DatabaseUnavailableError, sqlite3.Error, json.JSONDecodeError):
            raise CalibrationReviewPersistenceError from None

    def get(self, review_id: str) -> CalibrationReviewRecord:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute("SELECT * FROM calibration_reviews WHERE review_id = ?", (review_id,)).fetchone()
            if not row:
                raise CalibrationReviewNotFoundError(review_id)
            return self._from_row(row)
        except CalibrationReviewNotFoundError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error, json.JSONDecodeError):
            raise CalibrationReviewPersistenceError from None

    def list_for_session(self, session_id: str) -> list[CalibrationReviewRecord]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM calibration_reviews WHERE session_id = ? ORDER BY created_at DESC", (session_id,)
                ).fetchall()
            return [self._from_row(row) for row in rows]
        except (DatabaseUnavailableError, sqlite3.Error, json.JSONDecodeError):
            raise CalibrationReviewPersistenceError from None

    def sync(self, review_id: str, **values: object) -> CalibrationReviewRecord:
        allowed = {"hub_review_job_id", "status", "report", "review_package", "evidence_asset_ids", "contact_sheet_asset_ids", "error", "completed_at"}
        if not set(values) <= allowed:
            raise CalibrationReviewConflictError("invalid review update")
        json_fields = {"report", "review_package", "evidence_asset_ids", "contact_sheet_asset_ids"}
        assignments, params = [], []
        for key, value in values.items():
            column = f"{key}_json" if key in json_fields else key
            assignments.append(f"{column} = ?")
            params.append(json.dumps(value, ensure_ascii=False) if key in json_fields else value)
        assignments.append("updated_at = ?")
        params.extend([self.database.utc_now(), review_id])
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    f"UPDATE calibration_reviews SET {', '.join(assignments)} WHERE review_id = ?", params
                )
                if not cursor.rowcount:
                    raise CalibrationReviewNotFoundError(review_id)
                row = connection.execute("SELECT * FROM calibration_reviews WHERE review_id = ?", (review_id,)).fetchone()
                connection.commit()
            return self._from_row(row)
        except CalibrationReviewNotFoundError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationReviewPersistenceError from None

    def asset_belongs_to_session(self, session_id: str, asset_id: int) -> bool:
        self.database.initialize()
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM calibration_reviews WHERE session_id = ?", (session_id,)).fetchall()
            execution_rows = connection.execute("SELECT asset_ids_json FROM calibration_executions WHERE session_id = ?", (session_id,)).fetchall()
        for row in rows:
            record = self._from_row(row)
            if asset_id in {record.source_asset_id, *record.evidence_asset_ids, *record.contact_sheet_asset_ids}:
                return True
        return any(asset_id in json.loads(row["asset_ids_json"] or "[]") for row in execution_rows)

    def session_has_hard_errors(self, session_id: str) -> bool:
        return any(bool(record.report.get("hard_errors")) for record in self.list_for_session(session_id) if record.status == "completed")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CalibrationReviewRecord:
        return CalibrationReviewRecord(
            review_id=row["review_id"], client_request_id=row["client_request_id"], session_id=row["session_id"],
            execution_id=row["execution_id"], source_asset_id=int(row["source_asset_id"]),
            hub_review_job_id=row["hub_review_job_id"], status=row["status"], report=json.loads(row["report_json"] or "{}"),
            review_package=json.loads(row["review_package_json"] or "{}"),
            evidence_asset_ids=json.loads(row["evidence_asset_ids_json"] or "[]"),
            contact_sheet_asset_ids=json.loads(row["contact_sheet_asset_ids_json"] or "[]"), error=row["error"],
            created_at=row["created_at"], updated_at=row["updated_at"], completed_at=row["completed_at"],
        )
