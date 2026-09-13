"""Owner-set authority per operation: repository, API, and the runtime reading it."""
import httpx
import pytest
from atlas.api.app import app
from atlas.registry.repository import OperationAuthorityRepository


async def _request(method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_repository_sets_replaces_and_clears_decisions(pg_factory) -> None:
    async with pg_factory() as session:
        repository = OperationAuthorityRepository(session)
        assert await repository.overrides() == {}
        await repository.set("host.systemd.change_unit_state", "forbidden")
        await repository.set("schedules.create", "auto")
        await repository.set("schedules.create", "approval_required")
        with pytest.raises(ValueError):
            await repository.set("x", "loud")
        await session.commit()
        assert await repository.overrides() == {"host.systemd.change_unit_state": "forbidden", "schedules.create": "approval_required"}
        await repository.set("schedules.create", None)
        await repository.set("never.set", None)
        await session.commit()
        assert await repository.overrides() == {"host.systemd.change_unit_state": "forbidden"}


@pytest.mark.asyncio
async def test_control_lists_every_operation_with_default_and_effective_authority(monkeypatch) -> None:
    store = {"schedules.delete": "auto"}

    async def overrides(self):
        return dict(store)

    async def set_value(self, operation_id, authority):
        if authority is None:
            store.pop(operation_id, None)
        else:
            store[operation_id] = authority

    monkeypatch.setattr(OperationAuthorityRepository, "overrides", overrides)
    monkeypatch.setattr(OperationAuthorityRepository, "set", set_value)
    response = await _request("GET", "/api/control/operations")
    assert response.status_code == 200
    body = response.json()
    assert body["authorities"] == ["auto", "approval_required", "forbidden"]
    items = {item["id"]: item for item in body["items"]}
    delete = items["schedules.delete"]
    assert delete["default_authority"] == "approval_required" and delete["override"] == "auto" and delete["effective_authority"] == "auto"
    listing = items["schedules.list"]
    assert listing["override"] is None and listing["effective_authority"] == listing["default_authority"] == "auto"
    assert {"family", "effect", "enabled", "argument_rules", "description"} <= set(listing)

    updated = await _request("PUT", "/api/control/operations/schedules.list", json={"authority": "forbidden"})
    assert updated.status_code == 200 and updated.json()["effective_authority"] == "forbidden" and store["schedules.list"] == "forbidden"
    cleared = await _request("PUT", "/api/control/operations/schedules.list", json={"authority": None})
    assert cleared.status_code == 200 and cleared.json()["override"] is None and "schedules.list" not in store
    assert (await _request("PUT", "/api/control/operations/schedules.list", json={"authority": "loud"})).status_code == 422
    assert (await _request("PUT", "/api/control/operations/no.such.op", json={"authority": "auto"})).status_code == 404


@pytest.mark.asyncio
async def test_control_operations_sit_behind_the_owner_boundary(monkeypatch) -> None:
    import atlas.api.app as app_module

    monkeypatch.setattr(app_module.settings, "auth_required", True)
    assert (await _request("GET", "/api/control/operations")).status_code == 401
