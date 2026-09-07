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
    assert [item["id"] for item in capabilities] == ["atlas.artifacts", "atlas.local_storage", "atlas.project_folders", "atlas.schedules"]


def test_control_route_serves_spa_entrypoint() -> None:
    response = client.get("/control")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text

def test_context_pressure_states() -> None:
    from atlas.api.app import _context_pressure_state

    assert _context_pressure_state(699_999, 1_000_000) == "green"
    assert _context_pressure_state(700_000, 1_000_000) == "amber"
    assert _context_pressure_state(900_000, 1_000_000) == "red"


def test_control_restart_endpoint_requests_supervised_restart(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("atlas.api.app._restart_api_process", lambda: calls.append("restart"))

    response = client.post("/api/control/restart")

    assert response.status_code == 202
    assert response.json() == {"status": "restarting"}
    assert calls == ["restart"]
