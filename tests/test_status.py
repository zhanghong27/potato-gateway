from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

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


def test_versioned_action_schema_is_public_and_not_cached(client: TestClient) -> None:
    response = client.get("/potato-actions-v0.2.4.yaml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["x-potato-schema-build"] == "calibration-review-3.1-20260807"
    assert response.text.startswith("openapi: 3.1.0")
    assert "version: 0.2.4" in response.text
    assert "PublicObjectResponse" not in response.text


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
            {"id": "researcher", "display_name": "薯博士", "status": "unknown"},
            {"id": "creator", "display_name": "清蒸土豆", "status": "unknown"},
            {"id": "critic", "display_name": "酸辣土豆丝", "status": "unknown"},
            {"id": "engineer", "display_name": "薯码宝贝", "status": "unknown"},
        ],
    }


def test_status_success_response_does_not_contain_token(client: TestClient) -> None:
    response = client.get(
        "/api/status",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )

    assert TEST_TOKEN not in response.text


def test_empty_token_configuration_fails_fast() -> None:
    with pytest.raises(ValidationError, match="POTATO_GATEWAY_TOKEN"):
        Settings(POTATO_GATEWAY_TOKEN="")
