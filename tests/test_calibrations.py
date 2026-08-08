from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from potato_gateway.config import Settings
from potato_gateway.main import create_app


TEST_TOKEN = "calibration-test-token-" + "b" * 48
AGENT_DATA = {
    "researcher": ("薯博士", "research_agent", "potato-doctor"),
    "creator": ("清蒸土豆", "video_creator", "default"),
    "critic": ("酸辣土豆丝", "video_critic", "video-critic"),
}


@dataclass
class CalibrationFixture:
    client: TestClient
    database_path: Path
    root: Path


@pytest.fixture
def gateway(tmp_path: Path) -> CalibrationFixture:
    hermes_home = tmp_path / "hermes"
    registry_path = hermes_home / "gateway" / "config" / "agents.yaml"
    calibration_dir = hermes_home / "gateway" / "runtime" / "calibration"
    database_path = hermes_home / "gateway" / "runtime" / "gateway.db"
    registry_path.parent.mkdir(parents=True)
    calibration_dir.mkdir(parents=True)

    registry_lines = ["schema_version: 1", "", "agents:"]
    for agent_id, (display_name, role, profile_name) in AGENT_DATA.items():
        profile_path = f"profiles/{profile_name}"
        registry_lines.extend(
            [
                f"  {agent_id}:",
                f"    display_name: {display_name}",
                f"    role: {role}",
                f"    profile_name: {profile_name}",
                f"    hermes_profile: {profile_path}",
                "    prompt_files:",
                "      - SOUL.md",
            ]
        )
        profile_root = hermes_home / profile_path
        (profile_root / "skills" / f"skill-{agent_id}").mkdir(parents=True)
        (profile_root / "config.yaml").write_text(
            "\n".join(
                [
                    "model:",
                    "  provider: deepseek",
                    "  default: deepseek-v4-pro",
                    "  api_key: must-not-leak",
                    "memory:",
                    "  memory_enabled: true",
                    "secret: must-not-leak",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (profile_root / "SOUL.md").write_text(
            f"Prompt for {agent_id}.\n", encoding="utf-8"
        )

    registry_path.write_text("\n".join(registry_lines) + "\n", encoding="utf-8")
    settings = Settings(
        POTATO_GATEWAY_TOKEN=TEST_TOKEN,
        POTATO_HERMES_HOME=hermes_home,
        POTATO_AGENT_REGISTRY_PATH=registry_path,
        POTATO_CALIBRATION_STATE_DIR=calibration_dir,
        POTATO_GATEWAY_DB_PATH=database_path,
    )
    return CalibrationFixture(
        client=TestClient(create_app(settings)),
        database_path=database_path,
        root=tmp_path,
    )


def _headers(token: str = TEST_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _session_payload(
    request_id: str = "cal-create-researcher-001",
    agent_id: str = "researcher",
) -> dict:
    return {
        "client_request_id": request_id,
        "agent_id": agent_id,
        "goal": "Test research quality with cited sources",
        "acceptance_criteria": [
            "Cite information sources",
            "Explain the industry relationship",
        ],
    }


def _create_session(
    gateway: CalibrationFixture,
    request_id: str = "cal-create-researcher-001",
    agent_id: str = "researcher",
):
    return gateway.client.post(
        "/api/calibrations",
        headers=_headers(),
        json=_session_payload(request_id, agent_id),
    )


def _turn_payload(
    turn_id: str = "turn-researcher-001",
    actor: str = "commander",
    kind: str = "instruction",
) -> dict:
    return {
        "client_turn_id": turn_id,
        "actor": actor,
        "kind": kind,
        "content": "Research important commercial space events from the last two weeks.",
    }


def test_correct_token_creates_manual_calibrating_session(
    gateway: CalibrationFixture,
) -> None:
    response = _create_session(gateway)

    assert response.status_code == 201
    payload = response.json()
    assert payload["session_id"].startswith("cal_")
    assert payload["state"] == "calibrating"
    assert payload["transport"] == "manual"


def test_create_without_token_returns_401(gateway: CalibrationFixture) -> None:
    response = gateway.client.post("/api/calibrations", json=_session_payload())

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_create_with_wrong_token_returns_401(gateway: CalibrationFixture) -> None:
    response = gateway.client.post(
        "/api/calibrations",
        headers=_headers("wrong-token"),
        json=_session_payload(),
    )

    assert response.status_code == 401


def test_invalid_agent_returns_404(gateway: CalibrationFixture) -> None:
    response = _create_session(gateway, agent_id="invalid")

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


def test_create_snapshots_current_prompt_version(gateway: CalibrationFixture) -> None:
    profile = gateway.client.get(
        "/api/agents/researcher/profile", headers=_headers()
    ).json()

    session = _create_session(gateway).json()

    assert session["base_prompt_version"] == profile["prompt"]["version"]
    assert (
        session["base_prompt_content_sha256"]
        == profile["prompt"]["content_sha256"]
    )


def test_session_creation_is_idempotent(gateway: CalibrationFixture) -> None:
    first = _create_session(gateway)
    second = _create_session(gateway)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    with sqlite3.connect(gateway.database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM calibration_sessions"
        ).fetchone()[0]
    assert count == 1


def test_idempotent_create_returns_original_payload(gateway: CalibrationFixture) -> None:
    first = _create_session(gateway).json()
    changed = _session_payload()
    changed["goal"] = "A different goal on retry"

    response = gateway.client.post(
        "/api/calibrations", headers=_headers(), json=changed
    )

    assert response.status_code == 200
    assert response.json() == first


def test_idempotent_create_does_not_reread_profile(
    gateway: CalibrationFixture,
) -> None:
    first = _create_session(gateway).json()
    profile_root = gateway.root / "hermes" / "profiles" / "potato-doctor"
    profile_root.rename(gateway.root / "profile-temporarily-unavailable")

    response = _create_session(gateway)

    assert response.status_code == 200
    assert response.json() == first


def test_different_request_ids_create_different_sessions(
    gateway: CalibrationFixture,
) -> None:
    first = _create_session(gateway, "request-one").json()
    second = _create_session(gateway, "request-two").json()

    assert first["session_id"] != second["session_id"]


def test_session_can_be_queried_with_turns(gateway: CalibrationFixture) -> None:
    created = _create_session(gateway).json()
    gateway.client.post(
        f"/api/calibrations/{created['session_id']}/turns",
        headers=_headers(),
        json=_turn_payload(),
    )

    response = gateway.client.get(
        f"/api/calibrations/{created['session_id']}", headers=_headers()
    )

    assert response.status_code == 200
    assert response.json()["session_id"] == created["session_id"]
    assert len(response.json()["turns"]) == 1


def test_missing_session_returns_404(gateway: CalibrationFixture) -> None:
    response = gateway.client.get(
        "/api/calibrations/cal_missing", headers=_headers()
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "actor", ["user", "commander", "agent", "evaluator", "system"]
)
def test_all_supported_turn_actors_are_recorded(
    gateway: CalibrationFixture, actor: str
) -> None:
    session_id = _create_session(gateway).json()["session_id"]
    response = gateway.client.post(
        f"/api/calibrations/{session_id}/turns",
        headers=_headers(),
        json=_turn_payload(turn_id=f"turn-{actor}", actor=actor),
    )

    assert response.status_code == 201
    assert response.json()["actor"] == actor


def test_invalid_turn_actor_is_rejected(gateway: CalibrationFixture) -> None:
    session_id = _create_session(gateway).json()["session_id"]
    response = gateway.client.post(
        f"/api/calibrations/{session_id}/turns",
        headers=_headers(),
        json=_turn_payload(actor="robot"),
    )

    assert response.status_code == 422


def test_invalid_turn_kind_is_rejected(gateway: CalibrationFixture) -> None:
    session_id = _create_session(gateway).json()["session_id"]
    response = gateway.client.post(
        f"/api/calibrations/{session_id}/turns",
        headers=_headers(),
        json=_turn_payload(kind="evaluation"),
    )

    assert response.status_code == 422


@pytest.mark.parametrize("content", ["", "   ", "x" * 50_001])
def test_invalid_turn_content_is_rejected(
    gateway: CalibrationFixture, content: str
) -> None:
    session_id = _create_session(gateway).json()["session_id"]
    payload = _turn_payload()
    payload["content"] = content

    response = gateway.client.post(
        f"/api/calibrations/{session_id}/turns",
        headers=_headers(),
        json=payload,
    )

    assert response.status_code == 422


def test_turn_creation_is_idempotent(gateway: CalibrationFixture) -> None:
    session_id = _create_session(gateway).json()["session_id"]
    url = f"/api/calibrations/{session_id}/turns"
    first = gateway.client.post(url, headers=_headers(), json=_turn_payload())
    second = gateway.client.post(url, headers=_headers(), json=_turn_payload())

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    with sqlite3.connect(gateway.database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM calibration_turns"
        ).fetchone()[0]
    assert count == 1


def test_missing_session_cannot_receive_turn(gateway: CalibrationFixture) -> None:
    response = gateway.client.post(
        "/api/calibrations/cal_missing/turns",
        headers=_headers(),
        json=_turn_payload(),
    )

    assert response.status_code == 404


@pytest.mark.parametrize("state", ["closed", "blocked"])
def test_non_writable_session_rejects_turn(
    gateway: CalibrationFixture, state: str
) -> None:
    session_id = _create_session(gateway).json()["session_id"]
    with sqlite3.connect(gateway.database_path) as connection:
        connection.execute(
            "UPDATE calibration_sessions SET state = ? WHERE session_id = ?",
            (state, session_id),
        )
        connection.commit()

    response = gateway.client.post(
        f"/api/calibrations/{session_id}/turns",
        headers=_headers(),
        json=_turn_payload(),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Calibration session is not writable"}


def test_agent_session_list_is_newest_first(gateway: CalibrationFixture) -> None:
    created_ids = [
        _create_session(gateway, f"request-{index}").json()["session_id"]
        for index in range(3)
    ]

    response = gateway.client.get(
        "/api/agents/researcher/calibrations?limit=20", headers=_headers()
    )

    assert response.status_code == 200
    returned_ids = [item["session_id"] for item in response.json()["sessions"]]
    assert returned_ids == list(reversed(created_ids))


@pytest.mark.parametrize("limit", [0, 101])
def test_agent_session_limit_is_validated(
    gateway: CalibrationFixture, limit: int
) -> None:
    response = gateway.client.get(
        f"/api/agents/researcher/calibrations?limit={limit}", headers=_headers()
    )

    assert response.status_code == 422


def test_agent_session_limit_is_applied(gateway: CalibrationFixture) -> None:
    for index in range(3):
        _create_session(gateway, f"limit-request-{index}")

    response = gateway.client.get(
        "/api/agents/researcher/calibrations?limit=2", headers=_headers()
    )

    assert len(response.json()["sessions"]) == 2


def test_profile_reflects_active_calibrating_session(
    gateway: CalibrationFixture,
) -> None:
    session = _create_session(gateway).json()

    profile = gateway.client.get(
        "/api/agents/researcher/profile", headers=_headers()
    ).json()

    assert profile["calibration"]["state"] == "calibrating"
    assert profile["calibration"]["latest_session_id"] == session["session_id"]


def test_profile_reflects_blocked_session(gateway: CalibrationFixture) -> None:
    session_id = _create_session(gateway).json()["session_id"]
    with sqlite3.connect(gateway.database_path) as connection:
        connection.execute(
            "UPDATE calibration_sessions SET state = 'blocked' WHERE session_id = ?",
            (session_id,),
        )
        connection.commit()

    profile = gateway.client.get(
        "/api/agents/researcher/profile", headers=_headers()
    ).json()

    assert profile["calibration"]["state"] == "blocked"


def test_profile_with_only_closed_sessions_is_untracked(
    gateway: CalibrationFixture,
) -> None:
    session_id = _create_session(gateway).json()["session_id"]
    with sqlite3.connect(gateway.database_path) as connection:
        connection.execute(
            "UPDATE calibration_sessions SET state = 'closed' WHERE session_id = ?",
            (session_id,),
        )
        connection.commit()

    profile = gateway.client.get(
        "/api/agents/researcher/profile", headers=_headers()
    ).json()

    assert profile["calibration"]["state"] == "untracked"


def test_unknown_fields_and_local_paths_are_rejected(
    gateway: CalibrationFixture,
) -> None:
    unknown = _session_payload()
    unknown["unexpected"] = "value"
    path_payload = _session_payload("path-request")
    path_payload["goal"] = "Read /Users/example/private.txt"

    unknown_response = gateway.client.post(
        "/api/calibrations", headers=_headers(), json=unknown
    )
    path_response = gateway.client.post(
        "/api/calibrations", headers=_headers(), json=path_payload
    )

    assert unknown_response.status_code == 422
    assert path_response.status_code == 422
    assert "/Users/example" not in path_response.text


def test_request_and_acceptance_limits_are_enforced(
    gateway: CalibrationFixture,
) -> None:
    bad_id = _session_payload(request_id="contains space")
    too_many = _session_payload(request_id="too-many")
    too_many["acceptance_criteria"] = ["criterion"] * 21
    too_long = _session_payload(request_id="too-long")
    too_long["acceptance_criteria"] = ["x" * 1001]

    responses = [
        gateway.client.post("/api/calibrations", headers=_headers(), json=payload)
        for payload in (bad_id, too_many, too_long)
    ]

    assert [response.status_code for response in responses] == [422, 422, 422]


def test_original_endpoints_do_not_regress(gateway: CalibrationFixture) -> None:
    health = gateway.client.get("/health")
    status_response = gateway.client.get("/api/status", headers=_headers())
    profile = gateway.client.get("/api/agents/critic/profile", headers=_headers())

    assert health.status_code == 200
    assert status_response.status_code == 200
    assert profile.status_code == 200


def test_responses_do_not_expose_credentials_or_local_paths(
    gateway: CalibrationFixture,
) -> None:
    session = _create_session(gateway)
    text = session.text.lower()

    assert TEST_TOKEN not in session.text
    assert "api_key" not in text
    assert "secret" not in text
    assert str(gateway.root) not in session.text
    assert "/Users/zhanghong" not in session.text


def test_concurrent_session_retries_create_one_row(
    gateway: CalibrationFixture,
) -> None:
    def create() -> tuple[int, str]:
        response = _create_session(gateway, "concurrent-session")
        payload = response.json()
        assert "session_id" in payload, (response.status_code, payload)
        return response.status_code, payload["session_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: create(), range(8)))

    assert {session_id for _, session_id in results}.__len__() == 1
    assert sum(status_code == 201 for status_code, _ in results) == 1
    with sqlite3.connect(gateway.database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM calibration_sessions"
        ).fetchone()[0]
    assert count == 1


def test_concurrent_turn_retries_create_one_row(gateway: CalibrationFixture) -> None:
    session_id = _create_session(gateway).json()["session_id"]

    def create() -> tuple[int, str]:
        response = gateway.client.post(
            f"/api/calibrations/{session_id}/turns",
            headers=_headers(),
            json=_turn_payload(turn_id="concurrent-turn"),
        )
        return response.status_code, response.json()["turn_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: create(), range(8)))

    assert len({turn_id for _, turn_id in results}) == 1
    assert sum(status_code == 201 for status_code, _ in results) == 1
    with sqlite3.connect(gateway.database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM calibration_turns"
        ).fetchone()[0]
    assert count == 1


def test_database_schema_and_pragmas_are_initialized(
    gateway: CalibrationFixture,
) -> None:
    _create_session(gateway)
    with sqlite3.connect(gateway.database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        migration = connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    with gateway.client.app.state.database.connection() as application_connection:
        foreign_keys = application_connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0]

    assert {"schema_migrations", "calibration_sessions", "calibration_turns"} <= tables
    assert "idx_calibration_sessions_agent_id" in indexes
    assert "idx_calibration_turns_session_id" in indexes
    assert migration == 1
    assert journal_mode == "wal"
    assert foreign_keys == 1


def test_openapi_contains_all_calibration_operation_ids(
    gateway: CalibrationFixture,
) -> None:
    schema = gateway.client.get("/openapi.json").json()

    assert schema["paths"]["/api/calibrations"]["post"]["operationId"] == (
        "createCalibrationSession"
    )
    assert schema["paths"]["/api/calibrations/{session_id}"]["get"][
        "operationId"
    ] == "getCalibrationSession"
    assert schema["paths"]["/api/calibrations/{session_id}/turns"]["post"][
        "operationId"
    ] == "recordCalibrationTurn"
    assert schema["paths"]["/api/agents/{agent_id}/calibrations"]["get"][
        "operationId"
    ] == "listAgentCalibrations"
    assert schema["paths"]["/health"]["get"]["operationId"] == "getGatewayHealth"
    assert schema["paths"]["/api/status"]["get"]["operationId"] == (
        "getPotatoSystemStatus"
    )
    assert schema["paths"]["/api/agents/{agent_id}/profile"]["get"][
        "operationId"
    ] == "getAgentProfile"
