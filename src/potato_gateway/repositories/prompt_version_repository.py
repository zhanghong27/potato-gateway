from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass

from potato_gateway.database import Database, DatabaseUnavailableError


class PromptVersionPersistenceError(Exception):
    pass


class PromptVersionNotFoundError(Exception):
    pass


class PromptVersionConflictError(Exception):
    pass


def prompt_content_sha256(content: str) -> str:
    normalized = unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptVersionRecord:
    prompt_version_id: str
    client_request_id: str
    agent_id: str
    status: str
    content: str
    content_sha256: str
    base_content_sha256: str
    change_summary: str
    calibration_session_id: str
    created_at: str
    updated_at: str
    activated_at: str


class PromptVersionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_active_snapshot(
        self,
        *,
        agent_id: str,
        content: str,
        profile_content_sha256: str,
    ) -> PromptVersionRecord:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM prompt_versions WHERE agent_id = ? AND status = 'active' ORDER BY activated_at DESC LIMIT 1",
                    (agent_id,),
                ).fetchone()
                if existing:
                    return self._from_row(existing)
                now = self.database.utc_now()
                version_id = f"pv_{uuid.uuid4().hex}"
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO prompt_versions(
                        prompt_version_id, client_request_id, agent_id, status,
                        content, content_sha256, base_content_sha256, change_summary,
                        created_at, updated_at, activated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, 'Imported current Prompt', ?, ?, ?)
                    """,
                    (
                        version_id,
                        f"snapshot.{agent_id}.{profile_content_sha256}",
                        agent_id,
                        content,
                        prompt_content_sha256(content),
                        profile_content_sha256,
                        now,
                        now,
                        now,
                    ),
                )
                row = connection.execute("SELECT * FROM prompt_versions WHERE prompt_version_id = ?", (version_id,)).fetchone()
                connection.commit()
            return self._from_row(row)
        except (DatabaseUnavailableError, sqlite3.Error):
            raise PromptVersionPersistenceError from None

    def create_candidate(
        self,
        *,
        client_request_id: str,
        agent_id: str,
        content: str,
        base_content_sha256: str,
        change_summary: str,
        calibration_session_id: str,
    ) -> tuple[PromptVersionRecord, bool]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT * FROM prompt_versions WHERE client_request_id = ?", (client_request_id,)).fetchone()
                if existing:
                    record = self._from_row(existing)
                    if record.agent_id != agent_id:
                        raise PromptVersionConflictError(client_request_id)
                    connection.commit()
                    return record, False
                now = self.database.utc_now()
                version_id = f"pv_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO prompt_versions(
                        prompt_version_id, client_request_id, agent_id, status,
                        content, content_sha256, base_content_sha256, change_summary,
                        calibration_session_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (version_id, client_request_id, agent_id, content, prompt_content_sha256(content), base_content_sha256, change_summary, calibration_session_id, now, now),
                )
                row = connection.execute("SELECT * FROM prompt_versions WHERE prompt_version_id = ?", (version_id,)).fetchone()
                connection.commit()
            return self._from_row(row), True
        except PromptVersionConflictError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise PromptVersionPersistenceError from None

    def get(self, prompt_version_id: str) -> PromptVersionRecord:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                row = connection.execute("SELECT * FROM prompt_versions WHERE prompt_version_id = ?", (prompt_version_id,)).fetchone()
            if not row:
                raise PromptVersionNotFoundError(prompt_version_id)
            return self._from_row(row)
        except PromptVersionNotFoundError:
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise PromptVersionPersistenceError from None

    def list(self, agent_id: str, limit: int = 100) -> list[PromptVersionRecord]:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM prompt_versions WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
                    (agent_id, max(1, min(limit, 200))),
                ).fetchall()
            return [self._from_row(row) for row in rows]
        except (DatabaseUnavailableError, sqlite3.Error):
            raise PromptVersionPersistenceError from None

    def activate(self, prompt_version_id: str) -> PromptVersionRecord:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                target = connection.execute("SELECT * FROM prompt_versions WHERE prompt_version_id = ?", (prompt_version_id,)).fetchone()
                if not target:
                    raise PromptVersionNotFoundError(prompt_version_id)
                if target["status"] not in {"draft", "testing", "retired"}:
                    raise PromptVersionConflictError("version is not promotable")
                now = self.database.utc_now()
                connection.execute("UPDATE prompt_versions SET status = 'retired', updated_at = ? WHERE agent_id = ? AND status = 'active'", (now, target["agent_id"]))
                connection.execute("UPDATE prompt_versions SET status = 'active', updated_at = ?, activated_at = ? WHERE prompt_version_id = ?", (now, now, prompt_version_id))
                row = connection.execute("SELECT * FROM prompt_versions WHERE prompt_version_id = ?", (prompt_version_id,)).fetchone()
                connection.commit()
            return self._from_row(row)
        except (PromptVersionNotFoundError, PromptVersionConflictError):
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise PromptVersionPersistenceError from None

    def mark_testing(self, prompt_version_id: str) -> PromptVersionRecord:
        self.database.initialize()
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                target = connection.execute(
                    "SELECT * FROM prompt_versions WHERE prompt_version_id = ?",
                    (prompt_version_id,),
                ).fetchone()
                if not target:
                    raise PromptVersionNotFoundError(prompt_version_id)
                if target["status"] not in {"draft", "testing"}:
                    raise PromptVersionConflictError("only a draft can enter testing")
                connection.execute(
                    "UPDATE prompt_versions SET status = 'testing', updated_at = ? WHERE prompt_version_id = ?",
                    (self.database.utc_now(), prompt_version_id),
                )
                row = connection.execute(
                    "SELECT * FROM prompt_versions WHERE prompt_version_id = ?",
                    (prompt_version_id,),
                ).fetchone()
                connection.commit()
            return self._from_row(row)
        except (PromptVersionNotFoundError, PromptVersionConflictError):
            raise
        except (DatabaseUnavailableError, sqlite3.Error):
            raise PromptVersionPersistenceError from None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PromptVersionRecord:
        return PromptVersionRecord(
            prompt_version_id=row["prompt_version_id"], client_request_id=row["client_request_id"],
            agent_id=row["agent_id"], status=row["status"], content=row["content"],
            content_sha256=row["content_sha256"], base_content_sha256=row["base_content_sha256"],
            change_summary=row["change_summary"], calibration_session_id=row["calibration_session_id"],
            created_at=row["created_at"], updated_at=row["updated_at"], activated_at=row["activated_at"],
        )
