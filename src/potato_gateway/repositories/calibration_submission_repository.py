from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from potato_gateway.database import Database, DatabaseUnavailableError


class CalibrationSubmissionPersistenceError(Exception):
    pass


class CalibrationSubmissionNotFoundError(Exception):
    pass


class CalibrationSubmissionConflictError(Exception):
    pass


@dataclass(frozen=True)
class CalibrationSubmissionRecord:
    submission_id: str
    client_request_id: str
    session_id: str
    source_type: str
    execution_id: str
    primary_video_asset_id: int
    support_assets: list[dict[str, object]]
    source_id: str
    parent_submission_id: str
    status: str
    created_at: str
    updated_at: str


class CalibrationSubmissionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_existing(
        self,
        *,
        client_request_id: str,
        session_id: str,
        primary_video_asset_id: int,
        support_assets: list[dict[str, object]],
        source_id: str,
        parent_submission_id: str,
    ) -> tuple[CalibrationSubmissionRecord, bool]:
        return self._create(
            client_request_id=client_request_id,
            session_id=session_id,
            source_type="existing_assets",
            execution_id="",
            primary_video_asset_id=primary_video_asset_id,
            support_assets=support_assets,
            source_id=source_id,
            parent_submission_id=parent_submission_id,
        )

    def ensure_live(
        self,
        *,
        session_id: str,
        execution_id: str,
        primary_video_asset_id: int,
        support_assets: list[dict[str, object]],
    ) -> CalibrationSubmissionRecord:
        record, _created = self._create(
            client_request_id=f"live.{execution_id}",
            session_id=session_id,
            source_type="live_execution",
            execution_id=execution_id,
            primary_video_asset_id=primary_video_asset_id,
            support_assets=support_assets,
            source_id="",
            parent_submission_id="",
        )
        return record

    def _create(
        self,
        *,
        client_request_id: str,
        session_id: str,
        source_type: str,
        execution_id: str,
        primary_video_asset_id: int,
        support_assets: list[dict[str, object]],
        source_id: str,
        parent_submission_id: str,
    ) -> tuple[CalibrationSubmissionRecord, bool]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM calibration_submissions WHERE client_request_id = ?",
                    (client_request_id,),
                ).fetchone()
                if existing:
                    record = self._from_row(existing)
                    if (
                        record.session_id != session_id
                        or record.source_type != source_type
                        or record.execution_id != execution_id
                        or record.primary_video_asset_id != primary_video_asset_id
                        or record.support_assets != support_assets
                        or record.source_id != source_id
                        or record.parent_submission_id != parent_submission_id
                    ):
                        raise CalibrationSubmissionConflictError(client_request_id)
                    connection.commit()
                    return record, False
                session = connection.execute(
                    "SELECT agent_id, state FROM calibration_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise CalibrationSubmissionNotFoundError(session_id)
                if session["agent_id"] != "creator" or session["state"] != "calibrating":
                    raise CalibrationSubmissionConflictError(
                        "only an active creator session accepts video submissions"
                    )
                if parent_submission_id:
                    parent = connection.execute(
                        "SELECT session_id FROM calibration_submissions WHERE submission_id = ?",
                        (parent_submission_id,),
                    ).fetchone()
                    if not parent or parent["session_id"] != session_id:
                        raise CalibrationSubmissionConflictError(
                            "parent submission must belong to the same session"
                        )
                submission_id = f"sub_{uuid.uuid4().hex}"
                now = self.database.utc_now()
                connection.execute(
                    """
                    INSERT INTO calibration_submissions(
                        submission_id, client_request_id, session_id, source_type,
                        execution_id, primary_video_asset_id, support_assets_json,
                        source_id, parent_submission_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                    """,
                    (
                        submission_id,
                        client_request_id,
                        session_id,
                        source_type,
                        execution_id,
                        primary_video_asset_id,
                        json.dumps(support_assets, ensure_ascii=False, sort_keys=True),
                        source_id,
                        parent_submission_id,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM calibration_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
                connection.commit()
            return self._from_row(row), True
        except (
            CalibrationSubmissionNotFoundError,
            CalibrationSubmissionConflictError,
        ):
            raise
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationSubmissionPersistenceError from None

    def get(self, submission_id: str) -> CalibrationSubmissionRecord:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM calibration_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
            if not row:
                raise CalibrationSubmissionNotFoundError(submission_id)
            return self._from_row(row)
        except CalibrationSubmissionNotFoundError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationSubmissionPersistenceError from None

    def list_for_session(self, session_id: str) -> list[CalibrationSubmissionRecord]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM calibration_submissions
                    WHERE session_id = ?
                    ORDER BY created_at DESC, submission_id DESC
                    """,
                    (session_id,),
                ).fetchall()
            return [self._from_row(row) for row in rows]
        except (DatabaseUnavailableError, sqlite3.Error, ValueError, TypeError):
            raise CalibrationSubmissionPersistenceError from None

    def set_status(
        self, submission_id: str, status: str
    ) -> CalibrationSubmissionRecord:
        if status not in {"ready", "reviewing", "completed", "failed"}:
            raise CalibrationSubmissionConflictError("invalid submission status")
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE calibration_submissions
                    SET status = ?, updated_at = ?
                    WHERE submission_id = ?
                    """,
                    (status, self.database.utc_now(), submission_id),
                )
                if cursor.rowcount == 0:
                    raise CalibrationSubmissionNotFoundError(submission_id)
                row = connection.execute(
                    "SELECT * FROM calibration_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
                connection.commit()
            return self._from_row(row)
        except (
            CalibrationSubmissionNotFoundError,
            CalibrationSubmissionConflictError,
        ):
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise CalibrationSubmissionPersistenceError from None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CalibrationSubmissionRecord:
        support_assets = json.loads(row["support_assets_json"] or "[]")
        if not isinstance(support_assets, list) or not all(
            isinstance(item, dict) for item in support_assets
        ):
            raise CalibrationSubmissionPersistenceError
        return CalibrationSubmissionRecord(
            submission_id=row["submission_id"],
            client_request_id=row["client_request_id"],
            session_id=row["session_id"],
            source_type=row["source_type"],
            execution_id=row["execution_id"],
            primary_video_asset_id=int(row["primary_video_asset_id"]),
            support_assets=support_assets,
            source_id=row["source_id"],
            parent_submission_id=row["parent_submission_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
