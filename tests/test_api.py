from atlas.api.app import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_bootstrap_endpoint() -> None:
    response = client.get("/api/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert body["identity"] == "Atlas"
    assert body["environment_registry_available"] is True


def test_registry_endpoint_exposes_enabled_projection_only() -> None:
    response = client.get("/api/registry")
    assert response.status_code == 200
    capabilities = response.json()["capabilities"]
    assert [item["id"] for item in capabilities] == ["atlas.artifacts"]


def test_control_route_serves_spa_entrypoint() -> None:
    response = client.get("/control")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text
