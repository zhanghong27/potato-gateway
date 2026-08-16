from __future__ import annotations

import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from potato_gateway.adapters import HubClient, HubConflictError, HubUnavailableError, sanitize_hub_payload
from potato_gateway.adapters.hub_client import HubStreamResponse
from potato_gateway.config import Settings
from potato_gateway.main import create_app
from potato_gateway.services.prompt_version_service import PromptVersionService


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


def test_calibration_page_handles_non_json_http_errors(
    gateway: tuple[TestClient, Path],
) -> None:
    client, _ = gateway

    page = client.get("/calibrations")

    assert page.status_code == 200
    assert "const raw=await r.text()" in page.text
    assert "服务暂时不可用（HTTP ${r.status}）" in page.text


def test_submission_review_maps_hub_conflict_to_json_409(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, hermes_home = gateway
    session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "review-conflict-session",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Review a recovered delivery",
            "acceptance_criteria": [],
        },
    ).json()
    with sqlite3.connect(hermes_home / "gateway" / "gateway.db") as connection:
        now = "2026-08-16T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO calibration_submissions(
                submission_id, client_request_id, session_id, source_type,
                execution_id, primary_video_asset_id, support_assets_json,
                source_id, parent_submission_id, status, created_at, updated_at
            ) VALUES ('sub-conflict', 'sub-conflict-key', ?, 'existing_assets',
                '', 41, '[]', 'source-1', '', 'ready', ?, ?)
            """,
            (session["session_id"], now, now),
        )

    def reject_review(self, method, path, payload=None, *, headers=None, sanitize=True):
        raise HubConflictError("source delivery cannot be reviewed")

    monkeypatch.setattr(HubClient, "request", reject_review)
    response = client.post(
        f"/api/calibrations/{session['session_id']}/submissions/sub-conflict/reviews",
        headers=headers(),
        json={"client_request_id": "review-conflict"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "source delivery cannot be reviewed"


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


def test_calibration_session_can_be_archived_and_restored(
    gateway: tuple[TestClient, Path],
) -> None:
    client, _ = gateway
    created = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "archive-session-1",
            "agent_id": "creator",
            "transport": "manual",
            "goal": "Archive lifecycle",
            "acceptance_criteria": [],
        },
    ).json()
    session_id = created["session_id"]

    archived = client.delete(
        f"/api/calibrations/{session_id}", headers=headers()
    )
    assert archived.status_code == 200
    assert archived.json()["state"] == "closed"
    detail = client.get(
        f"/api/calibrations/{session_id}", headers=headers()
    )
    assert detail.json()["state"] == "closed"

    restored = client.post(
        f"/api/calibrations/{session_id}/restore", headers=headers()
    )
    assert restored.status_code == 200
    assert restored.json()["state"] == "calibrating"


def test_active_calibration_session_cannot_be_archived(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        assert method == "POST"
        assert path == "/api/calibration-jobs"
        return {"calibration_job": {"job_id": "caljob-active", "status": "queued"}}

    monkeypatch.setattr(HubClient, "request", fake_request)
    created = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "active-archive-session-1",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Keep active work writable",
            "acceptance_criteria": [],
        },
    ).json()
    session_id = created["session_id"]
    queued = client.post(
        f"/api/calibrations/{session_id}/executions",
        headers=headers(),
        json={"client_turn_id": "active-turn-1", "instruction": "Run now"},
    )
    assert queued.status_code == 202

    archived = client.delete(
        f"/api/calibrations/{session_id}", headers=headers()
    )
    assert archived.status_code == 409
    assert "active" in archived.json()["detail"]


def test_existing_delivery_submission_skips_creator_and_queues_critic(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway
    calls: list[tuple[str, str, dict | None]] = []
    review_attempts = 0

    def asset(asset_id: int, title: str, asset_type: str, role: str) -> dict:
        return {
            "id": asset_id,
            "asset_type": asset_type,
            "title": title,
            "mime_type": "video/mp4" if asset_type == "video" else "application/json",
            "file_size": 100,
            "width": 1080 if asset_type == "video" else 0,
            "height": 1920 if asset_type == "video" else 0,
            "duration_seconds": 15 if asset_type == "video" else 0,
            "status": "active",
            "available": True,
            "suggested_role": role,
            "preview_available": asset_type != "video",
        }

    primary = asset(42, "final.mp4", "video", "other")
    storyboard = asset(43, "storyboard.json", "document", "storyboard")

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        nonlocal review_attempts
        calls.append((method, path, payload))
        if method == "GET" and path == "/api/calibration-asset-sources/history-1":
            return {
                "source": {
                    "source_id": "history-1",
                    "source_type": "session",
                    "title": "Historical delivery",
                    "updated_at": "2026-08-08T00:00:00Z",
                    "recommended_video_asset_id": 42,
                    "assets": [primary, storyboard],
                }
            }
        if method == "GET" and path == "/api/assets/42/calibration-preview":
            return {"preview": {"asset": primary, "text_preview": "", "truncated": False}}
        if method == "GET" and path == "/api/assets/43/calibration-preview":
            return {
                "preview": {
                    "asset": storyboard,
                    "text_preview": '{"shots": []}',
                    "truncated": False,
                }
            }
        if method == "POST" and path == "/api/calibration-review-jobs":
            review_attempts += 1
            if review_attempts == 1:
                raise HubUnavailableError("temporary outage")
            assert payload["source_type"] == "existing_assets"
            assert payload["review_context"] == {
                "goal": "Review an existing delivery",
                "acceptance_criteria": ["Keep the video visually dynamic"],
                "user_feedback": [],
            }
            assert payload["support_assets"] == [
                {"asset_id": 43, "role": "storyboard"}
            ]
            return {
                "calibration_review_job": {
                    "review_job_id": "historical-review-job",
                    "status": "queued",
                }
            }
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(HubClient, "request", fake_request)
    session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "historical-session-1",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Review an existing delivery",
            "acceptance_criteria": ["Keep the video visually dynamic"],
        },
    ).json()
    created = client.post(
        f"/api/calibrations/{session['session_id']}/submissions",
        headers=headers(),
        json={
            "client_request_id": "historical-submission-1",
            "primary_video_asset_id": 42,
            "support_assets": [{"asset_id": 43, "role": "storyboard"}],
            "source_id": "history-1",
        },
    )
    assert created.status_code == 201
    submission = created.json()
    assert submission["source_type"] == "existing_assets"
    assert submission["execution_id"] is None
    assert submission["support_assets"][0]["text_preview"] == '{"shots": []}'
    assert not any(path == "/api/calibration-jobs" for _, path, _ in calls)

    replay = client.post(
        f"/api/calibrations/{session['session_id']}/submissions",
        headers=headers(),
        json={
            "client_request_id": "historical-submission-1",
            "primary_video_asset_id": 42,
            "support_assets": [{"asset_id": 43, "role": "storyboard"}],
            "source_id": "history-1",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["submission_id"] == submission["submission_id"]

    conflicting_replay = client.post(
        f"/api/calibrations/{session['session_id']}/submissions",
        headers=headers(),
        json={
            "client_request_id": "historical-submission-1",
            "primary_video_asset_id": 42,
            "support_assets": [],
            "source_id": "history-1",
        },
    )
    assert conflicting_replay.status_code == 409

    preview = client.get(
        "/api/calibration-asset-sources/history-1/assets/43/preview",
        headers=headers(),
    )
    assert preview.status_code == 200
    assert preview.json()["text_preview"] == '{"shots": []}'

    first_review = client.post(
        f"/api/calibrations/{session['session_id']}/submissions/{submission['submission_id']}/reviews",
        headers=headers(),
        json={"client_request_id": "historical-review-1"},
    )
    assert first_review.status_code == 503

    review = client.post(
        f"/api/calibrations/{session['session_id']}/submissions/{submission['submission_id']}/reviews",
        headers=headers(),
        json={"client_request_id": "historical-review-1"},
    )
    assert review.status_code == 200
    assert review.json()["submission_id"] == submission["submission_id"]
    assert review_attempts == 2


def test_existing_delivery_requires_available_primary_video(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = gateway

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        if "calibration-asset-sources" in path:
            return {
                "source": {
                    "source_id": "history-bad",
                    "source_type": "session",
                    "title": "Bad source",
                    "updated_at": "",
                    "recommended_video_asset_id": None,
                    "assets": [],
                }
            }
        return {
            "preview": {
                "asset": {
                    "id": 50,
                    "asset_type": "document",
                    "title": "storyboard.json",
                    "mime_type": "application/json",
                    "available": True,
                    "suggested_role": "storyboard",
                },
                "text_preview": "{}",
                "truncated": False,
            }
        }

    monkeypatch.setattr(HubClient, "request", fake_request)
    session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "bad-primary-session",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Reject invalid primary",
            "acceptance_criteria": [],
        },
    ).json()
    response = client.post(
        f"/api/calibrations/{session['session_id']}/submissions",
        headers=headers(),
        json={
            "client_request_id": "bad-primary-submission",
            "primary_video_asset_id": 50,
            "support_assets": [],
            "source_id": "history-bad",
        },
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
        elif method == "GET" and path == "/api/assets/41/summary":
            result = {
                "asset": {
                    "id": 41,
                    "asset_type": "video",
                    "mime_type": "video/mp4",
                    "title": "candidate.mp4",
                    "status": "active",
                    "available": True,
                }
            }
        else:
            raise AssertionError((method, path, payload))
        return sanitize_hub_payload(result) if sanitize else result

    monkeypatch.setattr(HubClient, "request", fake_request)
    def fake_stream(self, path, *, range_header=""):
        if range_header:
            return HubStreamResponse(
                response=BytesIO(b"pe"),
                status_code=206,
                headers={
                    "Content-Type": "image/jpeg",
                    "Content-Length": "2",
                    "Content-Range": "bytes 1-2/4",
                    "Accept-Ranges": "bytes",
                },
            )
        return HubStreamResponse(
            response=BytesIO(b"jpeg"),
            status_code=200,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": "4",
                "Accept-Ranges": "bytes",
            },
        )

    monkeypatch.setattr(HubClient, "open_stream", fake_stream)
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
    ui_link = client.get(
        f"/api/calibrations/{session['session_id']}/assets/41/link",
        headers=headers(),
    ).json()
    assert ui_link["available"] is True
    assert ui_link["status"] == "active"
    assert ui_link["playback_error"] == ""
    assert ui_link["url"].startswith("/api/calibration-evidence/41?")
    assert "tailscale" not in ui_link["url"]
    signed = urlparse(evidence["contact_sheets"][0]["url"])
    asset_response = client.get(f"{signed.path}?{signed.query}")
    assert asset_response.status_code == 200
    assert asset_response.headers["content-type"] == "image/jpeg"
    partial = client.get(
        f"{signed.path}?{signed.query}", headers={"Range": "bytes=1-2"}
    )
    assert partial.status_code == 206
    assert partial.content == b"pe"
    assert partial.headers["content-range"] == "bytes 1-2/4"
    assert partial.headers["accept-ranges"] == "bytes"
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


def test_legacy_candidate_with_case_specific_retest_rules_cannot_be_promoted(
    gateway: tuple[TestClient, Path],
) -> None:
    client, _hermes_home = gateway
    created = client.post(
        "/api/agents/creator/prompt-versions",
        headers=headers(),
        json={
            "client_request_id": "legacy-retest-candidate",
            "content": (
                "creator prompt\n\n"
                "## Retest acceptance\n"
                "- At 12 seconds show the current test asset.\n"
            ),
            "change_summary": "Legacy mixed capability and retest candidate",
        },
    )
    assert created.status_code == 201
    candidate = created.json()
    assert client.post(
        f"/api/admin/agents/creator/prompt-versions/{candidate['prompt_version_id']}/testing",
        headers=headers(),
    ).status_code == 200

    promoted = client.post(
        f"/api/admin/agents/creator/prompt-versions/{candidate['prompt_version_id']}/promote",
        headers=headers(),
        json={"confirm_content_sha256": candidate["content_sha256"]},
    )
    assert promoted.status_code == 409
    assert "case-specific retest rules" in promoted.json()["detail"]


def test_generated_prompt_candidate_runs_in_an_isolated_profile(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, hermes_home = gateway
    queued_payload: dict = {}

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        if method == "POST" and path == "/api/calibration-jobs":
            queued_payload.update(payload or {})
            return {"calibration_job": {"job_id": "candidate-job-1", "status": "queued"}}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(HubClient, "request", fake_request)
    session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "generated-candidate-session",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Avoid slideshow-like video",
            "acceptance_criteria": ["Use genuine motion in every major scene"],
        },
    ).json()
    client.post(
        f"/api/calibrations/{session['session_id']}/turns",
        headers=headers(),
        json={
            "client_turn_id": "candidate-user-feedback",
            "actor": "user",
            "kind": "critique",
            "content": "Do not use image zooms as a substitute for motion.",
        },
    )
    prompt_path = hermes_home / "profiles" / "video-creator" / "SOUL.md"
    original = prompt_path.read_text(encoding="utf-8")

    generated = client.post(
        f"/api/calibrations/{session['session_id']}/prompt-candidates",
        headers=headers(),
        json={
            "client_request_id": "generated-candidate-1",
            "additional_guidance": "Prefer camera and subject movement.",
        },
    )
    assert generated.status_code == 201
    candidate = generated.json()
    assert candidate["status"] == "draft"
    assert "Use genuine motion" in candidate["managed_addendum"]
    assert "Do not use image zooms" in candidate["managed_addendum"]
    assert prompt_path.read_text(encoding="utf-8") == original

    detail = client.get(
        f"/api/admin/agents/creator/prompt-versions/{candidate['prompt_version_id']}",
        headers=headers(),
    )
    assert detail.status_code == 200
    assert detail.json()["content"].startswith(original)
    assert detail.json()["content"].endswith(
        "<!-- POTATO CALIBRATION ADDENDUM END -->\n"
    )
    assert (
        "/api/admin/agents/{agent_id}/prompt-versions/{prompt_version_id}"
        not in client.get("/openapi.json").json()["paths"]
    )

    queued = client.post(
        f"/api/calibrations/{session['session_id']}/prompt-candidates/{candidate['prompt_version_id']}/tests",
        headers=headers(),
        json={
            "client_turn_id": "generated-candidate-test-1",
        },
    )
    assert queued.status_code == 202
    assert queued.json()["prompt_version_id"] == candidate["prompt_version_id"]
    assert queued_payload["prompt_version_id"] == candidate["prompt_version_id"]
    assert "Avoid slideshow-like video" in queued_payload["instruction"]
    assert "Use genuine motion in every major scene" in queued_payload["instruction"]
    assert "本 Session 没有现场基准任务" in queued_payload["instruction"]
    profile_name = queued_payload["profile_override"]
    assert profile_name.startswith("potato-cal-creator-")
    isolated_prompt = hermes_home / "profiles" / profile_name / "SOUL.md"
    assert isolated_prompt.read_text(encoding="utf-8").endswith(
        "<!-- POTATO CALIBRATION ADDENDUM END -->\n"
    )
    assert prompt_path.read_text(encoding="utf-8") == original


def test_generated_addendum_keeps_style_metrics_out_of_blocking_rules() -> None:
    review = SimpleNamespace(
        report={
            "hard_errors": [{"fix": "Return a playable H.264 video"}],
            "revision_requirements": ["Aim for average frame difference above 5.0"],
            "style_findings": [{"recommendation": "Use more meaningful subject motion"}],
        }
    )

    addendum, summary = PromptVersionService._build_addendum(
        "Improve motion",
        ["Keep the video factual"],
        [review],
        [],
        "",
    )

    blocking = addendum.split("## Blocking requirements", 1)[1].split(
        "## Quality targets", 1
    )[0]
    quality = addendum.split("## Quality targets", 1)[1].split(
        "## Execution budget", 1
    )[0]
    assert "Keep the video factual" in blocking
    assert "Return a playable H.264 video" in blocking
    assert "average frame difference" not in blocking
    assert "average frame difference" in quality
    assert "meaningful subject motion" in quality
    assert "one corrective full render" in addendum
    assert "2 条硬性规则" in summary
    assert "2 条质量目标" in summary


def test_chatgpt_advisory_builds_candidate_from_distilled_patch(
    gateway: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, hermes_home = gateway
    queued_payload: dict[str, object] = {}
    tooling_payloads: list[dict[str, object]] = []

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        if method == "GET" and path == "/api/assets/41/calibration-preview":
            return {
                "preview": {
                    "asset": {
                        "id": 41,
                        "asset_type": "video",
                        "title": "candidate.mp4",
                        "mime_type": "video/mp4",
                        "available": True,
                        "suggested_role": "primary_video",
                    },
                    "text_preview": "",
                    "truncated": False,
                }
            }
        if method == "POST" and path == "/api/calibration-jobs":
            queued_payload.update(payload or {})
            return {"calibration_job": {"job_id": "job-advisor-retest"}}
        if method == "POST" and path == "/api/calibration-tooling-tasks":
            tooling_payloads.append(payload or {})
            return {"workflow": {"workflow_id": "wf-tooling-1"}, "created": True}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(HubClient, "request", fake_request)
    session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "advisor-session",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Make the video feel human and relevant",
            "acceptance_criteria": ["Use concrete human stories"],
        },
    ).json()
    session_id = session["session_id"]
    client.post(
        f"/api/calibrations/{session_id}/turns",
        headers=headers(),
        json={
            "client_turn_id": "advisor-feedback",
            "actor": "user",
            "kind": "critique",
            "content": "The video feels synthetic and emotionally distant.",
        },
    )
    database_path = hermes_home / "gateway" / "gateway.db"
    now = "2026-08-15T10:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO calibration_submissions(
                submission_id, client_request_id, session_id, source_type,
                execution_id, primary_video_asset_id, support_assets_json,
                source_id, parent_submission_id, status, created_at, updated_at
            ) VALUES ('sub-advisor', 'sub-advisor-request', ?, 'existing_assets',
                      '', 41, '[]', 'workflow:test', '', 'completed', ?, ?)
            """,
            (session_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO calibration_reviews(
                review_id, client_request_id, session_id, execution_id,
                source_asset_id, hub_review_job_id, status, report_json,
                review_package_json, evidence_asset_ids_json,
                contact_sheet_asset_ids_json, error, created_at, updated_at,
                completed_at, submission_id
            ) VALUES ('calrev-advisor', 'calrev-advisor-request', ?, '', 41, '',
                      'completed', ?, ?, '[101]', '[102]', '', ?, ?, ?, 'sub-advisor')
            """,
            (
                session_id,
                '{"summary":"Visually polished but emotionally distant","verdict":"revise","total_score":76,"hard_errors":[],"style_findings":[],"shot_assessments":[],"revision_requirements":[]}',
                '{"evidence":[{"asset_id":101,"shot_index":1,"position":"middle","timestamp_seconds":4.2}],"transcript_status":"available","mechanical_metrics":{"shot_count":5}}',
                now,
                now,
                now,
            ),
        )
        connection.commit()

    created = client.post(
        f"/api/calibrations/{session_id}/advisories",
        headers=headers(),
        json={
            "client_request_id": "advisor-request-1",
            "submission_id": "sub-advisor",
            "review_id": "calrev-advisor",
        },
    )
    assert created.status_code == 201
    advisory_id = created.json()["advisory_id"]
    assert created.json()["status"] == "pending"

    pending = client.get(
        "/api/calibration-advisories?status=pending", headers=headers()
    ).json()
    assert [item["advisory_id"] for item in pending["advisories"]] == [advisory_id]
    bundle = client.get(
        f"/api/calibration-advisories/{advisory_id}/bundle", headers=headers()
    )
    assert bundle.status_code == 200
    assert bundle.json()["submission"]["primary_video"]["asset_id"] == 41
    assert bundle.json()["critic_review"]["report"]["total_score"] == 76
    assert bundle.json()["evidence"]["openaiFileResponse"]
    assert bundle.json()["user_feedback"] == [
        "The video feels synthetic and emotionally distant."
    ]
    assert bundle.json()["calibration_history"] == []
    other_session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "advisor-other-session",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Unrelated calibration",
            "acceptance_criteria": [],
        },
    ).json()
    cross_session = client.post(
        f"/api/calibrations/{other_session['session_id']}/advisories",
        headers=headers(),
        json={
            "client_request_id": "advisor-cross-session",
            "submission_id": "sub-advisor",
            "review_id": "calrev-advisor",
        },
    )
    assert cross_session.status_code == 404

    analysis = {
        "executive_summary": "Replace abstract AI visuals with one relatable story.",
        "user_intent": "Make viewers recognize their own work in the first three seconds.",
        "strengths": ["The basic production pipeline is now playable."],
        "findings": [
            {
                "category": "audience_relevance",
                "severity": "high",
                "diagnosis": "The opening explains a system instead of a human problem.",
                "root_cause": "The script starts from architecture rather than audience stakes.",
                "why_it_matters": "Viewers have no reason to keep watching.",
                "evidence_asset_ids": [101],
                "time_ranges": ["00:00-00:04"],
            }
        ],
        "priority_actions": [
            {
                "priority": 1,
                "action": "Open on a concrete creator failure and its consequence.",
                "rationale": "A recognizable conflict creates immediate relevance.",
                "evidence_asset_ids": [101],
            }
        ],
        "stale_rules_to_drop": ["Do not preserve the old 26-second scene recipe."],
        "persistent_capability_gaps": [
            "Vertical composition repeatedly leaves the lower frame unused."
        ],
        "capability_patch": [
            "Begin with a concrete person, situation, and consequence before explaining the system.",
            "Use real sourced images or cases when the topic depends on audience empathy.",
        ],
        "retest_spec": {
            "instruction": "Create a 45-60 second video around one concrete creator story.",
            "acceptance_criteria": [
                "The first three seconds identify a person, problem, and stake."
            ],
            "regression_checks": [
                "The lower quarter is not left empty for more than one second."
            ],
        },
        "tooling_tasks": [
            {
                "title": "Add vertical composition preflight",
                "category": "qa",
                "severity": "high",
                "problem": "Repeated renders leave the lower frame unused.",
                "expected_outcome": "Representative frames are blocked before rendering when layout coverage is poor.",
                "acceptance_criteria": ["Report per-beat vertical coverage."],
                "evidence_asset_ids": [101],
            }
        ],
        "limitations": ["Direct motion quality still requires watching the rendered video."],
    }
    duplicate = client.post(
        f"/api/calibrations/{session_id}/advisories",
        headers=headers(),
        json={
            "client_request_id": "advisor-request-invalid",
            "submission_id": "sub-advisor",
            "review_id": "calrev-advisor",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["advisory_id"] == advisory_id
    invalid_analysis = {
        **analysis,
        "findings": [
            {**analysis["findings"][0], "evidence_asset_ids": [999999]}
        ],
    }
    invalid = client.post(
        f"/api/calibration-advisories/{advisory_id}/complete",
        headers=headers(),
        json=invalid_analysis,
    )
    assert invalid.status_code == 409

    completed = client.post(
        f"/api/calibration-advisories/{advisory_id}/complete",
        headers=headers(),
        json=analysis,
    )
    assert completed.status_code == 200
    result = completed.json()
    assert result["status"] == "completed"
    assert result["analysis"]["executive_summary"] == analysis["executive_summary"]
    assert result["prompt_version_id"]

    detail = client.get(
        f"/api/admin/agents/creator/prompt-versions/{result['prompt_version_id']}",
        headers=headers(),
    ).json()
    assert "# ChatGPT calibration patch" in detail["content"]
    assert analysis["capability_patch"][0] in detail["content"]
    assert analysis["retest_spec"]["instruction"] not in detail["content"]
    assert analysis["retest_spec"]["acceptance_criteria"][0] not in detail["content"]
    assert analysis["user_intent"] not in detail["content"]
    assert "Visually polished but emotionally distant" not in detail["content"]
    assert "Do not preserve the old 26-second scene recipe" not in detail["content"]
    assert tooling_payloads[0]["advisory_id"] == advisory_id
    assert tooling_payloads[0]["category"] == "qa"

    queued = client.post(
        f"/api/calibrations/{session_id}/prompt-candidates/{result['prompt_version_id']}/tests",
        headers=headers(),
        json={"client_turn_id": "advisor-retest-1", "instruction": ""},
    )
    assert queued.status_code == 202
    assert analysis["retest_spec"]["instruction"] in queued_payload["instruction"]
    assert analysis["retest_spec"]["acceptance_criteria"][0] in queued_payload["instruction"]
    assert analysis["retest_spec"]["regression_checks"][0] in queued_payload["instruction"]
    assert "自主选择一个能充分检验目标的具体题材" not in queued_payload["instruction"]


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


def test_creator_candidate_can_activate_after_its_own_review_clears_hard_errors(
    gateway: tuple[TestClient, Path],
) -> None:
    client, hermes_home = gateway
    session = client.post(
        "/api/calibrations",
        headers=headers(),
        json={
            "client_request_id": "resolved-session",
            "agent_id": "creator",
            "transport": "hub",
            "goal": "Resolve the original hard error",
            "acceptance_criteria": [],
        },
    ).json()
    candidate = client.post(
        "/api/agents/creator/prompt-versions",
        headers=headers(),
        json={
            "client_request_id": "resolved-candidate",
            "content": "resolved candidate prompt",
            "change_summary": "Resolve hard error",
            "calibration_session_id": session["session_id"],
        },
    ).json()
    database_path = hermes_home / "gateway" / "gateway.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO calibration_executions(
                execution_id, session_id, client_turn_id, agent_id, status,
                instruction, prompt_version_id, created_at, updated_at
            ) VALUES ('resolved-exec', ?, 'resolved-exec-turn', 'creator',
                'completed', 'Repeat baseline', ?, '2026-08-09T01:00:00+00:00',
                '2026-08-09T01:00:00+00:00')
            """,
            (session["session_id"], candidate["prompt_version_id"]),
        )
        connection.execute(
            """
            INSERT INTO calibration_reviews(
                review_id, client_request_id, session_id, execution_id,
                source_asset_id, status, report_json, created_at, updated_at,
                completed_at
            ) VALUES ('resolved-review', 'resolved-review-key', ?,
                'resolved-exec', 1, 'completed',
                '{"verdict":"pass","hard_errors":[]}',
                '2026-08-09T01:10:00+00:00', '2026-08-09T01:10:00+00:00',
                '2026-08-09T01:10:00+00:00')
            """,
            (session["session_id"],),
        )
        connection.commit()

    promoted = client.post(
        f"/api/admin/agents/creator/prompt-versions/{candidate['prompt_version_id']}/promote",
        headers=headers(),
        json={"confirm_content_sha256": candidate["content_sha256"]},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "active"


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
        "generatePromptCandidate",
        "testPromptCandidate",
        "listPromptVersions",
    } <= operation_ids
