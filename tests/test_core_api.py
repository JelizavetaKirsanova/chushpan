from chushpan_core.app import create_app
from fastapi.testclient import TestClient


def test_health_is_minimal_and_does_not_require_credentials():
    response = TestClient(create_app(lambda: "test-token")).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "chushpan-core",
        "version": "0.1.0",
    }


def test_status_requires_valid_bearer_token():
    client = TestClient(create_app(lambda: "test-token"))

    assert client.get("/v1/status").status_code == 401
    assert (
        client.get("/v1/status", headers={"Authorization": "Basic test-token"}).status_code == 401
    )
    assert client.get("/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/v1/status", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    assert response.json()["service"] == "chushpan-core"


def test_status_returns_unavailable_when_secret_cannot_be_loaded():
    def fail_to_load():
        raise RuntimeError("fixture failure")

    response = TestClient(create_app(fail_to_load)).get(
        "/v1/status", headers={"Authorization": "Bearer test-token"}
    )

    assert response.status_code == 503
    assert "fixture failure" not in response.text
