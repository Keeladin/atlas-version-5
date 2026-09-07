import json
import stat

import pytest
from atlas.config import Settings
from atlas.control.connections import (
    apply_managed_overrides,
    connection_paths,
    save_github_connection,
    save_google_connection,
    save_model_connection,
)


def test_managed_connections_survive_restart_without_exposing_secret_paths(tmp_path):
    settings = Settings(state_dir=tmp_path, openai_api_key_file=None, github_token_file=None,
        gws_credentials_file=None)
    save_model_connection(settings, "sk-test-" + "x" * 40, "fixture-model")
    save_github_connection(settings, "github_pat_" + "y" * 40, "fixture-owner")
    save_google_connection(settings, {"type": "authorized_user", "client_id": "client",
        "client_secret": "secret", "refresh_token": "refresh"})
    paths = connection_paths(settings)
    for name in ("openai", "github", "google", "metadata"):
        assert stat.S_IMODE(paths[name].stat().st_mode) == 0o600
    restarted = apply_managed_overrides(Settings(state_dir=tmp_path, openai_api_key_file=None,
        github_token_file=None, gws_credentials_file=None))
    assert restarted.openai_api_key == "sk-test-" + "x" * 40
    assert restarted.openai_model == "fixture-model"
    assert restarted.github_token_file == paths["github"]
    assert restarted.github_owner == "fixture-owner"
    assert restarted.gws_credentials_file == paths["google"]
    assert restarted.gws_config_dir == paths["google_config"]
    assert not (restarted.gws_config_dir / "credentials.enc").exists()
    metadata = json.loads(paths["metadata"].read_text())
    assert metadata == {"github_owner": "fixture-owner", "openai_model": "fixture-model"}


def test_google_connection_rejects_non_authorized_user_payload(tmp_path):
    settings = Settings(state_dir=tmp_path)
    with pytest.raises(ValueError, match="authorized_user"):
        save_google_connection(settings, {"type": "service_account", "client_id": "x"})
    assert not connection_paths(settings)["google"].exists()


@pytest.mark.asyncio
async def test_model_setup_verifies_before_persisting_and_never_returns_key(monkeypatch):
    import atlas.api.app as app_module

    calls = []
    async def verify(key, model):
        calls.append(("verify", key, model))
        return {"ok": True, "detail": "verified"}
    def save(settings, key, model):
        calls.append(("save", key, model))
    monkeypatch.setattr(app_module, "_verify_model_connection", verify)
    monkeypatch.setattr(app_module, "save_model_connection", save)
    request = app_module.ModelConnectionRequest(api_key="sk-test-" + "z" * 40, model="fixture-model")
    result = await app_module.configure_model_connection(request)
    assert [call[0] for call in calls] == ["verify", "save"]
    assert result == {"ok": True, "detail": "verified", "configured": True,
        "restart_required": False, "model": "fixture-model"}
    assert "sk-test" not in json.dumps(result)


@pytest.mark.asyncio
async def test_github_setup_verifies_before_persisting(monkeypatch):
    import atlas.api.app as app_module

    calls = []
    monkeypatch.setattr(app_module, "_verify_github_connection",
        lambda token: calls.append(("verify", token)) or {"ok": True, "detail": "verified"})
    monkeypatch.setattr(app_module, "save_github_connection",
        lambda settings, token, owner: calls.append(("save", token, owner)))
    request = app_module.GitHubConnectionRequest(token="github_pat_" + "g" * 40, owner="fixture-owner")
    result = await app_module.configure_github_connection(request)
    assert [call[0] for call in calls] == ["verify", "save"]
    assert result["restart_required"] is True
    assert "github_pat" not in json.dumps(result)


@pytest.mark.asyncio
async def test_google_setup_verifies_before_persisting(monkeypatch):
    import atlas.api.app as app_module

    calls = []
    credential = {"type": "authorized_user", "client_id": "client",
        "client_secret": "secret", "refresh_token": "refresh"}
    monkeypatch.setattr(app_module, "_verify_google_connection",
        lambda payload: calls.append(("verify", payload)) or {"ok": True, "detail": "verified"})
    monkeypatch.setattr(app_module, "save_google_connection",
        lambda settings, payload: calls.append(("save", payload)))
    result = await app_module.configure_google_connection(app_module.GoogleConnectionRequest(credentials=credential))
    assert [call[0] for call in calls] == ["verify", "save"]
    assert result["restart_required"] is True
    assert "refresh" not in json.dumps(result)
