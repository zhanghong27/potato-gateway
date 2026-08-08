from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from potato_gateway.config import Settings
from potato_gateway.main import create_app


TEST_TOKEN = "profile-test-token-" + "a" * 48
AGENTS = {
    "researcher": ("薯博士", "research_agent", "potato-doctor"),
    "creator": ("清蒸土豆", "video_creator", "default"),
    "critic": ("酸辣土豆丝", "video_critic", "video-critic"),
}
PROFILE_PATHS = {
    "researcher": "profiles/potato-doctor",
    "creator": "profiles/creator-default",
    "critic": "profiles/video-critic",
}


@dataclass
class GatewayFixture:
    root: Path
    hermes_home: Path
    registry_path: Path
    calibration_dir: Path
    database_path: Path
    client: TestClient

    def profile_root(self, agent_id: str) -> Path:
        return self.hermes_home / PROFILE_PATHS[agent_id]


def _registry_text(*, metadata_agent: str | None = None) -> str:
    lines = ["schema_version: 1", "", "agents:"]
    for agent_id, (display_name, role, profile_name) in AGENTS.items():
        lines.extend(
            [
                f"  {agent_id}:",
                f"    display_name: {display_name}",
                f"    role: {role}",
                f"    profile_name: {profile_name}",
                f"    hermes_profile: {PROFILE_PATHS[agent_id]}",
                "    prompt_files:",
                "      - SOUL.md",
            ]
        )
        if agent_id == metadata_agent:
            lines.append("    prompt_metadata_file: prompt-version.yaml")
    return "\n".join(lines) + "\n"


def _prompt_full_hash(source_file: str, content: str) -> str:
    normalized = unicodedata.normalize(
        "NFC", content.replace("\r\n", "\n").replace("\r", "\n")
    )
    digest = hashlib.sha256()
    digest.update(source_file.encode("utf-8"))
    digest.update(b"\0")
    digest.update(normalized.encode("utf-8"))
    digest.update(b"\0")
    return digest.hexdigest()


@pytest.fixture
def gateway(tmp_path: Path) -> GatewayFixture:
    hermes_home = tmp_path / "hermes"
    registry_path = hermes_home / "potato-gateway" / "config" / "agents.yaml"
    calibration_dir = hermes_home / "potato-gateway" / "runtime" / "calibration"
    database_path = hermes_home / "potato-gateway" / "runtime" / "gateway.db"
    registry_path.parent.mkdir(parents=True)
    calibration_dir.mkdir(parents=True)
    registry_path.write_text(_registry_text(), encoding="utf-8")

    for agent_id in AGENTS:
        profile_root = hermes_home / PROFILE_PATHS[agent_id]
        (profile_root / "skills" / f"skill-{agent_id}").mkdir(parents=True)
        (profile_root / "config.yaml").write_text(
            "\n".join(
                [
                    "model:",
                    "  provider: deepseek",
                    "  default: deepseek-v4-pro",
                    "  api_key: should-never-be-returned",
                    "memory:",
                    "  memory_enabled: true",
                    "secret: should-never-be-returned",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (profile_root / "SOUL.md").write_text(
            f"You are {agent_id}.\n", encoding="utf-8"
        )

    settings = Settings(
        POTATO_GATEWAY_TOKEN=TEST_TOKEN,
        POTATO_HERMES_HOME=hermes_home,
        POTATO_AGENT_REGISTRY_PATH=registry_path,
        POTATO_CALIBRATION_STATE_DIR=calibration_dir,
        POTATO_GATEWAY_DB_PATH=database_path,
    )
    return GatewayFixture(
        root=tmp_path,
        hermes_home=hermes_home,
        registry_path=registry_path,
        calibration_dir=calibration_dir,
        database_path=database_path,
        client=TestClient(create_app(settings)),
    )


def _get(gateway: GatewayFixture, agent_id: str, token: str = TEST_TOKEN):
    return gateway.client.get(
        f"/api/agents/{agent_id}/profile",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize("agent_id", ["researcher", "creator", "critic"])
def test_registered_agent_profile_is_returned(
    gateway: GatewayFixture, agent_id: str
) -> None:
    response = _get(gateway, agent_id)

    assert response.status_code == 200
    payload = response.json()
    display_name, role, profile_name = AGENTS[agent_id]
    assert payload["agent"] == {
        "id": agent_id,
        "display_name": display_name,
        "role": role,
    }
    assert payload["profile"] == {
        "provider": "hermes",
        "profile_name": profile_name,
        "load_status": "loaded",
        "model_provider": "deepseek",
        "model_name": "deepseek-v4-pro",
        "skills": [f"skill-{agent_id}"],
        "memory_enabled": True,
    }


def test_profile_without_authorization_returns_401(gateway: GatewayFixture) -> None:
    response = gateway.client.get("/api/agents/critic/profile")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_profile_with_wrong_token_returns_401(gateway: GatewayFixture) -> None:
    response = _get(gateway, "critic", token="wrong-token")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_unregistered_agent_returns_404(gateway: GatewayFixture) -> None:
    response = _get(gateway, "invalid")

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


def test_missing_registered_profile_returns_503(gateway: GatewayFixture) -> None:
    gateway.profile_root("critic").rename(gateway.root / "missing-profile")

    response = _get(gateway, "critic")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Agent profile data is temporarily unavailable"
    }


def test_missing_calibration_is_untracked(gateway: GatewayFixture) -> None:
    response = _get(gateway, "critic")

    assert response.status_code == 200
    assert response.json()["calibration"] == {
        "state": "untracked",
        "latest_session_id": None,
        "last_activity_at": None,
        "current_prompt_version": None,
        "candidate_prompt_version": None,
        "latest_evaluation": None,
        "message": "No structured calibration record exists yet",
    }


def test_valid_calibration_is_parsed(gateway: GatewayFixture) -> None:
    state = {
        "schema_version": 1,
        "agent_id": "critic",
        "state": "evaluating",
        "latest_session_id": "calibration-critic-001",
        "last_activity_at": "2026-08-02T15:30:00+08:00",
        "current_prompt_version": "sha256:123456789abc",
        "candidate_prompt_version": "critic-next",
        "latest_evaluation": {
            "evaluation_id": "eval-critic-001",
            "score": 76,
            "threshold": 90,
            "result": "failed",
        },
        "message": "Evaluation in progress",
    }
    (gateway.calibration_dir / "critic.json").write_text(
        json.dumps(state), encoding="utf-8"
    )

    response = _get(gateway, "critic")

    assert response.status_code == 200
    calibration = response.json()["calibration"]
    assert calibration["state"] == "evaluating"
    assert calibration["latest_evaluation"]["result"] == "failed"
    assert calibration["last_activity_at"] == "2026-08-02T15:30:00+08:00"


def test_invalid_calibration_state_returns_503(gateway: GatewayFixture) -> None:
    state = {
        "schema_version": 1,
        "agent_id": "critic",
        "state": "online",
        "message": "invalid",
    }
    (gateway.calibration_dir / "critic.json").write_text(
        json.dumps(state), encoding="utf-8"
    )

    response = _get(gateway, "critic")

    assert response.status_code == 503
    assert "online" not in response.text


def test_calibration_agent_mismatch_returns_503(gateway: GatewayFixture) -> None:
    state = {
        "schema_version": 1,
        "agent_id": "creator",
        "state": "stable",
        "message": "wrong agent",
    }
    (gateway.calibration_dir / "critic.json").write_text(
        json.dumps(state), encoding="utf-8"
    )

    response = _get(gateway, "critic")

    assert response.status_code == 503
    assert "creator" not in response.text


def test_prompt_hash_is_stable(gateway: GatewayFixture) -> None:
    first = _get(gateway, "researcher").json()["prompt"]
    second = _get(gateway, "researcher").json()["prompt"]

    assert first["version"] == second["version"]
    assert first["content_sha256"] == second["content_sha256"]
    assert first["version_source"] == "content_hash"


def test_prompt_hash_changes_with_content(gateway: GatewayFixture) -> None:
    first = _get(gateway, "creator").json()["prompt"]["version"]
    (gateway.profile_root("creator") / "SOUL.md").write_text(
        "Changed prompt content.\n", encoding="utf-8"
    )

    second = _get(gateway, "creator").json()["prompt"]["version"]

    assert first != second


def test_verified_prompt_metadata_takes_precedence(gateway: GatewayFixture) -> None:
    prompt_content = (gateway.profile_root("critic") / "SOUL.md").read_text(
        encoding="utf-8"
    )
    full_hash = _prompt_full_hash("SOUL.md", prompt_content)
    gateway.registry_path.write_text(
        _registry_text(metadata_agent="critic"), encoding="utf-8"
    )
    (gateway.profile_root("critic") / "prompt-version.yaml").write_text(
        "\n".join(
            [
                "schema_version: 1",
                "version: critic-2026-08-02",
                f"content_sha256: {full_hash}",
                "updated_at: 2026-08-02T15:30:00+08:00",
                "source_files:",
                "  - SOUL.md",
                "",
            ]
        ),
        encoding="utf-8",
    )

    response = _get(gateway, "critic")

    assert response.status_code == 200
    prompt = response.json()["prompt"]
    assert prompt["version"] == "critic-2026-08-02"
    assert prompt["version_source"] == "metadata"
    assert prompt["content_sha256"] == full_hash[:12]


def test_unverified_prompt_metadata_returns_503(gateway: GatewayFixture) -> None:
    gateway.registry_path.write_text(
        _registry_text(metadata_agent="critic"), encoding="utf-8"
    )
    (gateway.profile_root("critic") / "prompt-version.yaml").write_text(
        "\n".join(
            [
                "schema_version: 1",
                "version: invented-version",
                f"content_sha256: {'0' * 64}",
                "updated_at: 2026-08-02T15:30:00+08:00",
                "source_files: [SOUL.md]",
                "",
            ]
        ),
        encoding="utf-8",
    )

    response = _get(gateway, "critic")

    assert response.status_code == 503
    assert "invented-version" not in response.text


def test_response_uses_whitelist_and_contains_no_secrets_or_paths(
    gateway: GatewayFixture,
) -> None:
    response = _get(gateway, "researcher")

    assert response.status_code == 200
    lowered = response.text.lower()
    assert "api_key" not in lowered
    assert "token" not in lowered
    assert "secret" not in lowered
    assert str(gateway.root) not in response.text
    assert "/Users/zhanghong" not in response.text


def test_unsafe_model_identifier_is_not_returned(gateway: GatewayFixture) -> None:
    (gateway.profile_root("researcher") / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: deepseek",
                "  default: https://models.example/model?token=sensitive",
                "memory:",
                "  memory_enabled: true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    response = _get(gateway, "researcher")

    assert response.status_code == 200
    assert response.json()["profile"]["model_name"] is None
    assert "sensitive" not in response.text


def test_registry_path_traversal_returns_503(gateway: GatewayFixture) -> None:
    gateway.registry_path.write_text(
        _registry_text().replace(
            "hermes_profile: profiles/potato-doctor",
            "hermes_profile: ../outside",
        ),
        encoding="utf-8",
    )

    response = _get(gateway, "researcher")

    assert response.status_code == 503
    assert "outside" not in response.text


def test_prompt_symlink_cannot_escape_hermes_root(gateway: GatewayFixture) -> None:
    outside_prompt = gateway.root / "outside-prompt.md"
    outside_prompt.write_text("external-sensitive-content", encoding="utf-8")
    prompt_path = gateway.profile_root("researcher") / "SOUL.md"
    prompt_path.unlink()
    prompt_path.symlink_to(outside_prompt)

    response = _get(gateway, "researcher")

    assert response.status_code == 503
    assert "external-sensitive-content" not in response.text


def test_openapi_contains_agent_operation_and_existing_operation_ids(
    gateway: GatewayFixture,
) -> None:
    schema = gateway.client.get("/openapi.json").json()
    operation = schema["paths"]["/api/agents/{agent_id}/profile"]["get"]

    assert operation["operationId"] == "getAgentProfile"
    assert operation["parameters"][0]["schema"]["enum"] == [
        "researcher",
        "creator",
        "critic",
        "engineer",
    ]
    assert schema["paths"]["/health"]["get"]["operationId"] == "getGatewayHealth"
    assert (
        schema["paths"]["/api/status"]["get"]["operationId"]
        == "getPotatoSystemStatus"
    )


def test_existing_health_and_status_behavior_is_unchanged(
    gateway: GatewayFixture,
) -> None:
    health = gateway.client.get("/health")
    status_response = gateway.client.get(
        "/api/status",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert status_response.status_code == 200
    assert [agent["status"] for agent in status_response.json()["agents"]] == [
        "unknown",
        "unknown",
        "unknown",
        "unknown",
    ]
