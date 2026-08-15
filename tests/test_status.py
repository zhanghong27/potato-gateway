from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from potato_gateway.adapters import HubClient
from potato_gateway.config import Settings
from potato_gateway.main import create_app


TEST_TOKEN = "a" * 64


@pytest.fixture
def client() -> TestClient:
    settings = Settings(POTATO_GATEWAY_TOKEN=TEST_TOKEN)
    return TestClient(create_app(settings))


def test_health_without_authentication(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_console_links_primary_surfaces(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "视频工作流" in response.text
    assert 'href="/calibrations"' in response.text
    assert 'href="/docs"' in response.text
    assert 'href="/potato-actions-v0.2.7.yaml"' in response.text
    assert "土豆状态" in response.text
    assert "activity_state" in response.text
    assert "animateAgentStates()" in response.text
    assert "prefers-reduced-motion: no-preference" in response.text


def test_calibration_console_exposes_session_lifecycle_controls(
    client: TestClient,
) -> None:
    response = client.get("/calibrations")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "新建校准" in response.text
    assert "createSession()" in response.text
    assert "archiveSession()" in response.text
    assert "restoreSession()" in response.text
    assert "executeTest()" in response.text
    assert "历史交付" in response.text
    assert "createImportedSubmission()" in response.text
    assert "captureDetailUi()" in response.text
    assert "restoreDetailUi(ui)" in response.text
    assert "nextSignature!==detailSignature" in response.text
    assert "基准现场测试" in response.text
    assert "当前校准轮次" in response.text
    assert "成片与交付" in response.text
    assert "评审与证据" in response.text
    assert "Prompt 改进" in response.text
    assert "重新获取播放链接" in response.text
    assert "候选版本产物" in response.text
    assert "generateCandidate()" in response.text
    assert "testCandidate(" in response.text
    assert "activateCandidate(" in response.text
    assert "额外复测要求" in response.text
    assert "你无需填写" in response.text
    assert "Gateway 会把校准目标" in response.text


def test_versioned_action_schema_is_public_and_not_cached(client: TestClient) -> None:
    response = client.get("/potato-actions-v0.2.7.yaml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert (
        response.headers["x-potato-schema-build"]
        == "optional-candidate-test-3.1-20260809"
    )
    assert response.text.startswith("openapi: 3.1.0")
    assert "version: 0.2.7" in response.text
    assert "PublicObjectResponse" not in response.text

    legacy_response = client.get("/potato-actions-v0.2.6.yaml")
    assert legacy_response.status_code == 200
    assert "version: 0.2.7" in legacy_response.text


def test_status_without_authorization_header_returns_401(client: TestClient) -> None:
    response = client.get("/api/status")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_status_with_wrong_token_returns_401(client: TestClient) -> None:
    response = client.get(
        "/api/status",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_status_with_correct_token_returns_200(client: TestClient) -> None:
    response = client.get(
        "/api/status",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )

    assert response.status_code == 200


def test_status_success_response_has_expected_structure(client: TestClient) -> None:
    response = client.get(
        "/api/status",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )

    payload = response.json()
    assert payload == {
        "service": {
            "name": "potato-gateway",
            "status": "running",
                "version": "0.2.0",
        },
        "potato_hub": {
            "status": "offline",
            "message": "Potato Hub is not reachable",
        },
        "agents": [
            {"id": "researcher", "display_name": "薯博士", "status": "unknown", "activity_state": "unknown", "activity_label": "状态暂时未知"},
            {"id": "creator", "display_name": "清蒸土豆", "status": "unknown", "activity_state": "unknown", "activity_label": "状态暂时未知"},
            {"id": "critic", "display_name": "酸辣土豆丝", "status": "unknown", "activity_state": "unknown", "activity_label": "状态暂时未知"},
            {"id": "engineer", "display_name": "薯码宝贝", "status": "unknown", "activity_state": "unknown", "activity_label": "状态暂时未知"},
        ],
    }


def test_status_describes_live_agent_activity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def fake_request(self, method, path, payload=None, *, headers=None, sanitize=True):
        if path == "/api/health":
            return {"ok": True}
        if path == "/api/agents":
            return {
                "heartbeats": [
                    {"agent_id": "researcher", "status": "busy", "current_work_item_id": "work_1", "metadata": {}, "updated_at": now},
                    {"agent_id": "creator", "status": "calibrating", "current_work_item_id": "caljob_1", "metadata": {"mode": "calibration"}, "updated_at": now},
                    {"agent_id": "critic", "status": "online", "current_work_item_id": "", "metadata": {}, "updated_at": now},
                    {"agent_id": "engineer", "status": "error", "current_work_item_id": "", "metadata": {}, "updated_at": now},
                ]
            }
        raise AssertionError((method, path))

    monkeypatch.setattr(HubClient, "request", fake_request)

    payload = client.get(
        "/api/status", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    ).json()
    activities = {item["id"]: item for item in payload["agents"]}

    assert activities["researcher"]["activity_state"] == "working"
    assert activities["researcher"]["activity_label"] == "搜集视频素材"
    assert activities["creator"]["activity_state"] == "calibrating"
    assert activities["creator"]["activity_label"] == "执行校准复测"
    assert activities["critic"]["activity_state"] == "idle"
    assert activities["critic"]["activity_label"] == "等着审片"
    assert activities["engineer"]["activity_state"] == "error"
    assert activities["engineer"]["activity_label"] == "上次任务异常"


def test_status_success_response_does_not_contain_token(client: TestClient) -> None:
    response = client.get(
        "/api/status",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )

    assert TEST_TOKEN not in response.text


def test_empty_token_configuration_fails_fast() -> None:
    with pytest.raises(ValidationError, match="POTATO_GATEWAY_TOKEN"):
        Settings(POTATO_GATEWAY_TOKEN="")
