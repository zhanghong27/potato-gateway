from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from potato_gateway.adapters import HubClient, sanitize_hub_payload
from potato_gateway.config import Settings
from potato_gateway.main import create_app


TOKEN = "gateway-integration-test-token-" + "x" * 48


def registry_text() -> str:
    return """schema_version: 1
agents:
  researcher:
    display_name: 薯博士
    role: research_agent
    profile_name: potato-doctor
    hermes_profile: profiles/potato-doctor
    prompt_files: [SOUL.md]
  creator:
    display_name: 清蒸土豆
    role: video_creator
    profile_name: default
    hermes_profile: profiles/video-creator
    prompt_files: [SOUL.md]
  critic:
    display_name: 酸辣土豆丝
    role: video_critic
    profile_name: video-critic
    hermes_profile: profiles/video-critic
    prompt_files: [SOUL.md]
  engineer:
    display_name: 薯码宝贝
    role: engineering_agent
    profile_name: code-potato
    hermes_profile: profiles/code-potato
    prompt_files: [SOUL.md]
"""


@pytest.fixture
def gateway(tmp_path: Path) -> tuple[TestClient, Path]:
    hermes_home = tmp_path / "hermes"
    registry = hermes_home / "gateway" / "agents.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(registry_text(), encoding="utf-8")
    for profile in ("potato-doctor", "video-creator", "video-critic", "code-potato"):
        root = hermes_home / "profiles" / profile
        root.mkdir(parents=True)
        root.joinpath("config.yaml").write_text(
            "model:\n  provider: test\n  default: test-model\nmemory:\n  memory_enabled: false\n",
            encoding="utf-8",
        )
        root.joinpath("SOUL.md").write_text(f"original prompt for {profile}\n", encoding="utf-8")
    settings = Settings(
        POTATO_GATEWAY_TOKEN=TOKEN,
        POTATO_HERMES_HOME=hermes_home,
        POTATO_AGENT_REGISTRY_PATH=registry,
        POTATO_CALIBRATION_STATE_DIR=hermes_home / "gateway" / "calibration",
        POTATO_GATEWAY_DB_PATH=hermes_home / "gateway" / "gateway.db",
        POTATO_HUB_URL="http://127.0.0.1:9",
    )
    with TestClient(create_app(settings)) as client:
        yield client, hermes_home


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_hub_calibration_turn_records_real_async_response(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        if method == "POST" and path == "/api/calibration-jobs":
            result = {"calibration_job": {"job_id": "caljob-1", "status": "queued"}}
        elif method == "GET" and path == "/api/calibration-jobs/caljob-1":
            result = {
                "calibration_job": {
                    "job_id": "caljob-1",
                    "status": "completed",
                    "response": "Rendered sample at /Users/example/private/final.mp4",
                    "asset_ids": [41],
                    "error": "",
                }
            }
        else:
            raise AssertionError((method, path, payload))
        return sanitize_hub_payload(result) if sanitize else result

    monkeypatch.setattr(HubClient, "request", fake_request)
    session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "hub-session-1",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Establish a video baseline",
            "acceptance_criteria": ["Return a real artifact"],
        },
    )
    assert session.status_code == 201
    session_id = session.json()["session_id"]
    queued = client.post(
        f"/api/calibrations/{session_id}/executions",
        headers=headers(),
        json={"client_turn_id": "baseline-turn-1", "instruction": "Create one sample."},
    )
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"

    execution_id = queued.json()["execution_id"]
    completed = client.get(
        f"/api/calibrations/{session_id}/executions/{execution_id}", headers=headers()
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["asset_ids"] == [41]
    assert "/Users/" not in completed.json()["response"]

    detail = client.get(f"/api/calibrations/{session_id}", headers=headers()).json()
    assert [turn["actor"] for turn in detail["turns"]] == ["commander", "agent"]


def test_manual_calibration_cannot_execute_agent(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway
    session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "manual-session-1",
            "agent_id": "creator",
            "goal": "Manual notes",
            "acceptance_criteria": [],
        },
    ).json()
    response = client.post(
        f"/api/calibrations/{session['session_id']}/executions",
        headers=headers(),
        json={"client_turn_id": "manual-turn-1", "instruction": "Do not run."},
    )
    assert response.status_code == 409


def test_creator_calibration_review_returns_scoped_signed_evidence(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        if method == "POST" and path == "/api/calibration-jobs":
            result = {"calibration_job": {"job_id": "caljob-review", "status": "queued"}}
        elif method == "GET" and path == "/api/calibration-jobs/caljob-review":
            result = {"calibration_job": {"status": "completed", "response": "Video ready", "asset_ids": [41], "error": ""}}
        elif method == "POST" and path == "/api/calibration-review-jobs":
            result = {"calibration_review_job": {"review_job_id": "hub-review-1", "status": "queued"}}
        elif method == "GET" and path == "/api/calibration-review-jobs/hub-review-1":
            result = {
                "calibration_review_job": {
                    "status": "completed",
                    "report": {
                        "summary": "Motion is too limited",
                        "verdict": "revise",
                        "total_score": 78,
                        "hard_errors": [],
                        "style_findings": [{"description": "PPT-like", "evidence_asset_ids": [101]}],
                        "shot_assessments": [{"description": "Static opening", "evidence_asset_ids": [101]}],
                    },
                    "review_package": {
                        "evidence": [{"asset_id": 101, "shot_index": 1, "position": "middle", "timestamp_seconds": 1.5}],
                        "transcript_status": "unavailable",
                        "mechanical_metrics": {"shot_count": 1},
                    },
                    "evidence_asset_ids": [101],
                    "contact_sheet_asset_ids": [102],
                    "error": "",
                    "completed_at": "2026-08-07T09:00:00+00:00",
                }
            }
        else:
            raise AssertionError((method, path, payload))
        return sanitize_hub_payload(result) if sanitize else result

    monkeypatch.setattr(HubClient, "request", fake_request)
    monkeypatch.setattr(HubClient, "request_bytes", lambda self, path: (b"jpeg", "image/jpeg", "evidence.jpg"))
    session = client.post(
        "/api/calibrations", headers=headers(),
        json={"client_request_id": "review-session", "agent_id": "creator", "transport": "hub", "goal": "Video baseline", "acceptance_criteria": []},
    ).json()
    queued = client.post(
        f"/api/calibrations/{session['session_id']}/executions", headers=headers(),
        json={"client_turn_id": "review-turn", "instruction": "Create a video"},
    ).json()
    completed = client.get(
        f"/api/calibrations/{session['session_id']}/executions/{queued['execution_id']}", headers=headers()
    ).json()
    review = client.post(
        f"/api/calibrations/{session['session_id']}/executions/{completed['execution_id']}/reviews",
        headers=headers(), json={"client_request_id": "critic-review-1", "source_asset_id": 41},
    )
    assert review.status_code == 202
    review_id = review.json()["review_id"]
    polled = client.get(
        f"/api/calibrations/{session['session_id']}/executions/{completed['execution_id']}/reviews/{review_id}", headers=headers()
    )
    assert polled.json()["status"] == "completed"
    evidence = client.get(
        f"/api/calibrations/{session['session_id']}/reviews/{review_id}/evidence", headers=headers()
    ).json()
    assert evidence["frames"][0]["description"] == "Static opening"
    assert len(evidence["openaiFileResponse"]) == 1
    signed = urlparse(evidence["contact_sheets"][0]["url"])
    asset_response = client.get(f"{signed.path}?{signed.query}")
    assert asset_response.status_code == 200
    assert asset_response.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/calibrations/not-this-session/reviews/{review_id}/evidence", headers=headers()).status_code == 404


def test_prompt_candidate_requires_explicit_hash_promotion_and_can_rollback(
    gateway: tuple[TestClient, Path],
) -> None:
    client, hermes_home = gateway
    prompt_path = hermes_home / "profiles" / "video-creator" / "SOUL.md"
    original = prompt_path.read_text(encoding="utf-8")
    candidate_content = "candidate creator prompt\nwith stricter video checks\n"
    created = client.post(
        "/api/agents/creator/prompt-versions",
        headers=headers(),
        json={
            "client_request_id": "prompt-candidate-1",
            "content": candidate_content,
            "change_summary": "Tighten video quality gates",
        },
    )
    assert created.status_code == 201
    candidate = created.json()
    assert prompt_path.read_text(encoding="utf-8") == original

    versions = client.get(
        "/api/agents/creator/prompt-versions", headers=headers()
    ).json()["versions"]
    assert len(versions) == 2
    assert candidate_content not in str(versions)
    original_version = next(item for item in versions if item["status"] == "active")

    testing = client.post(
        f"/api/admin/agents/creator/prompt-versions/{candidate['prompt_version_id']}/testing",
        headers=headers(),
    )
    assert testing.status_code == 200
    assert testing.json()["status"] == "testing"

    promoted = client.post(
        f"/api/admin/agents/creator/prompt-versions/{candidate['prompt_version_id']}/promote",
        headers=headers(),
        json={"confirm_content_sha256": candidate["content_sha256"]},
    )
    assert promoted.status_code == 200
    assert prompt_path.read_text(encoding="utf-8") == candidate_content

    rollback = client.post(
        f"/api/admin/agents/creator/prompt-versions/{original_version['prompt_version_id']}/rollback",
        headers=headers(),
        json={"confirm_content_sha256": original_version["content_sha256"]},
    )
    assert rollback.status_code == 200
    assert prompt_path.read_text(encoding="utf-8") == original
    assert "/api/admin/agents/{agent_id}/prompt-versions/{prompt_version_id}/promote" not in client.get("/openapi.json").json()["paths"]
    assert "/api/admin/agents/{agent_id}/prompt-versions/{prompt_version_id}/testing" not in client.get("/openapi.json").json()["paths"]
    assert "/api/admin/agents/{agent_id}/prompt-versions/{prompt_version_id}/rollback" not in client.get("/openapi.json").json()["paths"]


def test_creator_prompt_activation_is_blocked_by_critic_hard_error(
    gateway: tuple[TestClient, Path],
) -> None:
    client, hermes_home = gateway
    session = client.post(
        "/api/calibrations", headers=headers(),
        json={"client_request_id": "blocked-session", "agent_id": "creator", "goal": "Creator baseline", "acceptance_criteria": []},
    ).json()
    candidate = client.post(
        "/api/agents/creator/prompt-versions", headers=headers(),
        json={
            "client_request_id": "blocked-candidate", "content": "candidate prompt with changes",
            "change_summary": "Address creator baseline", "calibration_session_id": session["session_id"],
        },
    ).json()
    database_path = hermes_home / "gateway" / "gateway.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO calibration_reviews(
                review_id, client_request_id, session_id, execution_id, source_asset_id,
                status, report_json, created_at, updated_at, completed_at
            ) VALUES ('hard-review', 'hard-review-key', ?, 'exec-hard', 1, 'completed',
                '{"hard_errors":[{"code":"unplayable"}]}',
                '2026-08-07T09:00:00+00:00', '2026-08-07T09:00:00+00:00', '2026-08-07T09:00:00+00:00')
            """,
            (session["session_id"],),
        )
        connection.commit()
    promoted = client.post(
        f"/api/admin/agents/creator/prompt-versions/{candidate['prompt_version_id']}/promote",
        headers=headers(), json={"confirm_content_sha256": candidate["content_sha256"]},
    )
    assert promoted.status_code == 409
    assert "hard errors" in promoted.json()["detail"]


def test_workflow_action_uses_idempotency_and_sanitizes_hub_response(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway
    captured: dict = {}

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        captured.update({"method": method, "path": path, "payload": payload, "headers": headers})
        result = {
            "workflow": {
                "workflow_id": "wf-1",
                "status": "researching",
                "output": {"file_path": "/Users/example/private/video.mp4"},
            }
        }
        return sanitize_hub_payload(result) if sanitize else result

    monkeypatch.setattr(HubClient, "request", fake_request)
    response = client.post(
        "/api/workflows/video",
        headers=headers(),
        json={
            "client_request_id": "workflow-request-1",
            "title": "Sample",
            "request": "Make a factual vertical video.",
            "handoff_policy": {
                "research_to_creation": "manual",
                "creation_to_review": "auto",
                "review_to_revision": "manual",
            },
        },
    )
    assert response.status_code == 201
    assert captured["headers"]["X-Idempotency-Key"] == "workflow-request-1"
    assert captured["payload"]["handoff_policy"]["research_to_creation"] == "manual"
    assert captured["payload"]["handoff_policy"]["review_to_revision"] == "manual"
    assert "/Users/" not in response.text
    assert "file_path" not in response.text


def test_workflow_idempotent_replay_returns_200(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        return {"ok": True, "workflow": {"workflow_id": "wf-1"}, "created": False}

    monkeypatch.setattr(HubClient, "request", fake_request)
    response = client.post(
        "/api/workflows/video",
        headers=headers(),
        json={
            "client_request_id": "workflow-replay-1",
            "request": "Make a factual vertical video.",
        },
    )
    assert response.status_code == 200


def test_new_action_operation_ids_are_exposed(gateway: tuple[TestClient, Path]) -> None:
    client, _ = gateway
    schema = client.get("/openapi.json").json()
    operation_ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert {
        "createVideoWorkflow",
        "getWorkflow",
        "listWorkflowEvents",
        "sendWorkflowMessage",
        "decideWorkflowApproval",
        "getAssetSummary",
        "getVideoReview",
        "executeCalibrationTurn",
        "getCalibrationTurn",
        "createCalibrationReview",
        "getCalibrationReview",
        "getCalibrationEvidence",
        "createPromptCandidate",
        "listPromptVersions",
    } <= operation_ids
