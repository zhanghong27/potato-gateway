from __future__ import annotations

import sqlite3
import threading
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


MIGRATION_VERSION = 4
BUSY_TIMEOUT_MS = 5_000
LOGGER = logging.getLogger("potato_gateway.database")
APP_DATABASE_LOCK = threading.Lock()


class DatabaseUnavailableError(Exception):
    pass


class Database:
    def __init__(self, path: Path, hermes_home: Path) -> None:
        self.hermes_home = hermes_home.expanduser().resolve()
        self.path = self._resolve_database_path(path)
        self._initialized = False
        self._initialize_lock = threading.Lock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            with self.connection() as connection:
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA foreign_keys = OFF")
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_migrations (
                            version INTEGER PRIMARY KEY,
                            applied_at TEXT NOT NULL
                        )
                        """
                    )
                    applied = {
                        int(row["version"])
                        for row in connection.execute(
                            "SELECT version FROM schema_migrations"
                        ).fetchall()
                    }
                    if 1 not in applied:
                        self._apply_v1(connection)
                        connection.execute(
                            "INSERT INTO schema_migrations (version, applied_at) "
                            "VALUES (?, ?)",
                            (1, self.utc_now()),
                        )
                    if 2 not in applied:
                        self._apply_v2(connection)
                        connection.execute(
                            "INSERT INTO schema_migrations (version, applied_at) "
                            "VALUES (?, ?)",
                            (2, self.utc_now()),
                        )
                    if 3 not in applied:
                        self._apply_v3(connection)
                        connection.execute(
                            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                            (3, self.utc_now()),
                        )
                    if 4 not in applied:
                        self._apply_v4(connection)
                        connection.execute(
                            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                            (4, self.utc_now()),
                        )
                    connection.commit()
                    connection.execute("PRAGMA foreign_keys = ON")
                except sqlite3.Error as exc:
                    connection.rollback()
                    LOGGER.error("Database initialization failed: %s", exc)
                    raise DatabaseUnavailableError("database is unavailable") from None
            self._initialized = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            yield connection
        except (OSError, sqlite3.Error) as exc:
            LOGGER.error("Database connection failed: %s", exc)
            raise DatabaseUnavailableError("database is unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    def _apply_v1(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE calibration_sessions (
                session_id TEXT PRIMARY KEY,
                client_request_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('calibrating', 'blocked', 'closed')
                ),
                transport TEXT NOT NULL CHECK (transport = 'manual'),
                goal TEXT NOT NULL,
                acceptance_criteria_json TEXT NOT NULL,
                base_prompt_version TEXT NOT NULL,
                base_prompt_content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE calibration_turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                client_turn_id TEXT NOT NULL UNIQUE,
                actor TEXT NOT NULL CHECK (
                    actor IN ('user', 'commander', 'agent', 'evaluator', 'system')
                ),
                kind TEXT NOT NULL CHECK (
                    kind IN ('instruction', 'response', 'critique', 'note')
                ),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES calibration_sessions(session_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_calibration_sessions_agent_id "
            "ON calibration_sessions(agent_id)"
        )
        connection.execute(
            "CREATE INDEX idx_calibration_sessions_created_at "
            "ON calibration_sessions(created_at)"
        )
        connection.execute(
            "CREATE INDEX idx_calibration_sessions_agent_created "
            "ON calibration_sessions(agent_id, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_calibration_turns_session_id "
            "ON calibration_turns(session_id)"
        )
        connection.execute(
            "CREATE INDEX idx_calibration_turns_created_at "
            "ON calibration_turns(created_at)"
        )
        connection.execute(
            "CREATE INDEX idx_calibration_turns_session_created "
            "ON calibration_turns(session_id, created_at ASC)"
        )

    def _apply_v2(self, connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE calibration_turns RENAME TO calibration_turns_v1")
        connection.execute("ALTER TABLE calibration_sessions RENAME TO calibration_sessions_v1")
        connection.execute(
            """
            CREATE TABLE calibration_sessions (
                session_id TEXT PRIMARY KEY,
                client_request_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('calibrating', 'blocked', 'closed')
                ),
                transport TEXT NOT NULL CHECK (transport IN ('manual', 'hub')),
                goal TEXT NOT NULL,
                acceptance_criteria_json TEXT NOT NULL,
                base_prompt_version TEXT NOT NULL,
                base_prompt_content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE calibration_turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                client_turn_id TEXT NOT NULL UNIQUE,
                actor TEXT NOT NULL CHECK (
                    actor IN ('user', 'commander', 'agent', 'evaluator', 'system')
                ),
                kind TEXT NOT NULL CHECK (
                    kind IN ('instruction', 'response', 'critique', 'note')
                ),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES calibration_sessions(session_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO calibration_sessions
            SELECT * FROM calibration_sessions_v1
            """
        )
        connection.execute(
            """
            INSERT INTO calibration_turns
            SELECT * FROM calibration_turns_v1
            """
        )
        connection.execute("DROP TABLE calibration_turns_v1")
        connection.execute("DROP TABLE calibration_sessions_v1")
        connection.execute(
            """
            CREATE TABLE calibration_executions (
                execution_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                client_turn_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('queued', 'running', 'completed', 'failed')
                ),
                instruction TEXT NOT NULL,
                response TEXT NOT NULL DEFAULT '',
                asset_ids_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                hub_job_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES calibration_sessions(session_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE prompt_versions (
                prompt_version_id TEXT PRIMARY KEY,
                client_request_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('draft', 'testing', 'active', 'retired')
                ),
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                base_content_sha256 TEXT NOT NULL,
                change_summary TEXT NOT NULL,
                calibration_session_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                activated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute("CREATE INDEX idx_calibration_sessions_agent_id ON calibration_sessions(agent_id)")
        connection.execute("CREATE INDEX idx_calibration_sessions_created_at ON calibration_sessions(created_at)")
        connection.execute("CREATE INDEX idx_calibration_sessions_agent_created ON calibration_sessions(agent_id, created_at DESC)")
        connection.execute("CREATE INDEX idx_calibration_turns_session_id ON calibration_turns(session_id)")
        connection.execute("CREATE INDEX idx_calibration_turns_created_at ON calibration_turns(created_at)")
        connection.execute("CREATE INDEX idx_calibration_turns_session_created ON calibration_turns(session_id, created_at ASC)")
        connection.execute("CREATE INDEX idx_calibration_executions_session ON calibration_executions(session_id, created_at)")
        connection.execute("CREATE INDEX idx_prompt_versions_agent_status ON prompt_versions(agent_id, status, created_at DESC)")

    def _apply_v3(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE calibration_reviews (
                review_id TEXT PRIMARY KEY,
                client_request_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                source_asset_id INTEGER NOT NULL,
                hub_review_job_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (
                    status IN ('queued', 'preparing', 'reviewing', 'completed', 'failed')
                ),
                report_json TEXT NOT NULL DEFAULT '{}',
                review_package_json TEXT NOT NULL DEFAULT '{}',
                evidence_asset_ids_json TEXT NOT NULL DEFAULT '[]',
                contact_sheet_asset_ids_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_calibration_reviews_session ON calibration_reviews(session_id, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_calibration_reviews_execution ON calibration_reviews(execution_id, created_at DESC)"
        )

    def _apply_v4(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE calibration_submissions (
                submission_id TEXT PRIMARY KEY,
                client_request_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK (
                    source_type IN ('live_execution', 'existing_assets')
                ),
                execution_id TEXT NOT NULL DEFAULT '',
                primary_video_asset_id INTEGER NOT NULL,
                support_assets_json TEXT NOT NULL DEFAULT '[]',
                source_id TEXT NOT NULL DEFAULT '',
                parent_submission_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (
                    status IN ('ready', 'reviewing', 'completed', 'failed')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES calibration_sessions(session_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            "ALTER TABLE calibration_reviews ADD COLUMN submission_id TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "CREATE INDEX idx_calibration_submissions_session ON calibration_submissions(session_id, created_at DESC)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX idx_calibration_submissions_execution ON calibration_submissions(execution_id) WHERE execution_id != ''"
        )
        connection.execute(
            "CREATE INDEX idx_calibration_reviews_submission ON calibration_reviews(submission_id, created_at DESC)"
        )

    def _resolve_database_path(self, path: Path) -> Path:
        expanded = path.expanduser()
        candidate = expanded if expanded.is_absolute() else Path.cwd() / expanded
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.hermes_home)
        except ValueError:
            raise DatabaseUnavailableError("database is unavailable") from None
        return resolved

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()


def get_app_database(app: object, *, path: Path, hermes_home: Path) -> Database:
    state = getattr(app, "state")
    database = getattr(state, "database", None)
    if database is None:
        with APP_DATABASE_LOCK:
            database = getattr(state, "database", None)
            if database is None:
                database = Database(path=path, hermes_home=hermes_home)
                state.database = database
    return database
