import httpx
import pytest
from atlas.api.app import app
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


async def _request(method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_vapid_public_key_reports_unconfigured_by_default() -> None:
    response = await _request("GET", "/api/push/vapid-public-key")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False and body["public_key"] is None


@pytest.mark.asyncio
async def test_vapid_public_key_is_derived_from_the_secret_file(tmp_path, monkeypatch) -> None:
    import atlas.api.app as app_module

    key = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "vapid").write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setattr(app_module.settings, "push_vapid_private_key_file", tmp_path / "vapid")
    monkeypatch.setattr(app_module.settings, "push_vapid_subject", "mailto:owner@example.com")
    response = await _request("GET", "/api/push/vapid-public-key")
    body = response.json()
    assert body["configured"] is True and len(body["public_key"]) == 87 and body["subject"] == "mailto:owner@example.com"


@pytest.mark.asyncio
async def test_subscribe_validates_shape_before_touching_storage() -> None:
    response = await _request("POST", "/api/push/subscribe", json={"endpoint": "https://push.example/abc"})
    assert response.status_code == 422
    response = await _request("POST", "/api/push/subscribe",
        json={"endpoint": "http://push.example/abc", "keys": {"p256dh": "p", "auth": "a"}})
    assert response.status_code == 422
    assert "https" in response.json()["detail"]


@pytest.mark.asyncio
async def test_notification_and_push_routes_sit_behind_the_owner_boundary(monkeypatch) -> None:
    import atlas.api.app as app_module

    monkeypatch.setattr(app_module.settings, "auth_required", True)
    for path in ("/api/push/vapid-public-key", "/api/notifications", "/api/push/subscriptions"):
        response = await _request("GET", path)
        assert response.status_code == 401, path


def test_control_configuration_projects_notification_settings() -> None:
    import atlas.api.app as app_module

    assert app_module.settings.rdc_monitor_unit == "desktop-commander.service"
    assert app_module.settings.push_repeat_minutes == 60
