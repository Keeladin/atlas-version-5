from uuid import UUID, uuid4

import httpx
import pytest
from atlas.api.app import app
from atlas.integrations import workspace_tasks_mcp_server as mcp
from atlas.workspace import tasks


async def request(method, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def test_mcp_uses_shared_service():
    assert mcp.HANDLERS == {
        "create": tasks.create_task, "list": tasks.list_tasks, "get": tasks.get_task,
        "cancel": tasks.cancel_task, "resume": tasks.resume_task,
    }


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("GET", "/id"), ("POST", "/id/cancel"), ("POST", "/id/resume")])
async def test_workspace_requires_owner_auth(monkeypatch, method, suffix):
    monkeypatch.setattr("atlas.api.app.settings.auth_required", True)
    assert (await request(method, f"/api/workspace/tasks{suffix}")).status_code == 401


async def test_owner_lifecycle_requires_same_origin_in_production(monkeypatch):
    monkeypatch.setattr("atlas.api.app.settings.environment", "production")
    response = await request("POST", f"/api/workspace/tasks/{uuid4()}/cancel", headers={"origin": "https://other.example"})
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/workspace", "/workspace/"])
async def test_workspace_serves_spa(path, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text('<div id="root"></div>')
    monkeypatch.setattr("atlas.api.app.settings.frontend_dist", tmp_path)
    response = await request("GET", path)
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text


async def test_no_manual_create_route():
    assert (await request("POST", "/api/workspace/tasks", json={})).status_code == 405


@pytest.mark.parametrize("path", ["?status=unknown", "?limit=0", "?limit=101", "?offset=-1", "/not-a-uuid"])
async def test_invalid_inputs(path):
    assert (await request("GET", f"/api/workspace/tasks{path}")).status_code == 422


@pytest.mark.parametrize("operation", ["cancel", "resume"])
async def test_invalid_lifecycle_id(operation):
    assert (await request("POST", f"/api/workspace/tasks/bad/{operation}")).status_code == 422


@pytest.mark.parametrize("method,operation,suffix", [("GET", "get_task", ""), ("POST", "cancel_task", "/cancel"), ("POST", "resume_task", "/resume")])
@pytest.mark.parametrize("error,status", [(tasks.TaskNotFound("missing"), 404), (tasks.TaskConflict("conflict"), 409), (ValueError("invalid"), 422)])
async def test_service_errors_map_to_http(monkeypatch, method, operation, suffix, error, status):
    async def fail(_arguments):
        raise error
    monkeypatch.setattr(tasks, operation, fail)
    response = await request(method, f"/api/workspace/tasks/{uuid4()}{suffix}")
    assert response.status_code == status
    assert response.json()["detail"] == str(error)


async def test_list_passes_validated_filter_and_pagination(monkeypatch):
    async def list_tasks(arguments):
        assert arguments == {"status": "terminal", "limit": 2, "offset": 4}
        return {"items": [], "next_offset": None}
    monkeypatch.setattr(tasks, "list_tasks", list_tasks)
    response = await request("GET", "/api/workspace/tasks?status=terminal&limit=2&offset=4")
    assert response.json() == {"items": [], "next_offset": None}


@pytest.mark.parametrize("operation", ["cancel", "resume"])
async def test_direct_owner_lifecycle_uses_service_immediately(monkeypatch, operation):
    task_id = str(uuid4())
    async def change(arguments):
        assert arguments == {"task_id": task_id}
        return {"task_id": task_id, "status": "cancelled" if operation == "cancel" else "active"}
    monkeypatch.setattr(tasks, f"{operation}_task", change)
    response = await request("POST", f"/api/workspace/tasks/{task_id}/{operation}")
    assert response.status_code == 200
    assert response.json()["task_id"] == task_id


async def test_owner_rest_lifecycle_and_filtered_history(pg_factory, monkeypatch):
    monkeypatch.setattr(tasks, "get_session_factory", lambda: pg_factory)
    active = await tasks.create_task({"objective": "Keep working", "acceptance_criteria": ["Verified"]})
    terminal = await tasks.create_task({"objective": "Stop work", "acceptance_criteria": ["Verified"]})
    cancelled = await request("POST", f'/api/workspace/tasks/{terminal["task_id"]}/cancel')
    assert cancelled.json()["status"] == "cancelled"
    listed = await request("GET", "/api/workspace/tasks?status=active&limit=1")
    assert [item["task_id"] for item in listed.json()["items"]] == [active["task_id"]]
    terminal_page = await request("GET", "/api/workspace/tasks?status=terminal&limit=1")
    assert [item["task_id"] for item in terminal_page.json()["items"]] == [terminal["task_id"]]
    first = (await request("GET", "/api/workspace/tasks?limit=1")).json()
    assert first["next_offset"] == 1
    second = (await request("GET", "/api/workspace/tasks?limit=1&offset=1")).json()
    assert second["next_offset"] is None
    assert first["items"][0]["task_id"] != second["items"][0]["task_id"]
    for operation in ("cancel", "resume"):
        assert (await request("POST", f'/api/workspace/tasks/{terminal["task_id"]}/{operation}')).status_code == 409
    assert (await request("POST", f'/api/workspace/tasks/{active["task_id"]}/resume')).status_code == 409
    assert (await request("GET", f"/api/workspace/tasks/{uuid4()}")).status_code == 404
    fetched = await request("GET", f'/api/workspace/tasks/{active["task_id"].upper()}')
    assert fetched.json()["objective"] == active["objective"]


@pytest.mark.parametrize("operation", ["resume", "cancel"])
async def test_owner_lifecycle_resolves_stall_but_preserves_effect_attention(pg_factory, monkeypatch, operation):
    from atlas.persistence.models import OwnerAttentionRow, RunRow, TranscriptRow
    from atlas.transcript.repository import TranscriptRepository

    monkeypatch.setattr(tasks, "get_session_factory", lambda: pg_factory)
    created = await tasks.create_task({"objective": "Recover stalled task", "acceptance_criteria": ["Verified"]})
    async with pg_factory() as session:
        row = await session.get(TranscriptRow, UUID(created["transcript_id"]))
        state = dict(row.active_task_state)
        state["runtime"].update(controller_state="stalled", retry_count=3, transient_retry_count=48)
        await TranscriptRepository(session).update_active_task_state(row.id, state, expected_revision=row.active_task_revision)
        run = RunRow(transcript_id=row.id, kind="background", status="succeeded", inference_active=False)
        session.add(run)
        await session.flush()
        stall = OwnerAttentionRow(run_id=run.id, state="managed_task_stalled", title="Stalled", detail={})
        uncertain = OwnerAttentionRow(run_id=run.id, state="uncertain", title="Uncertain effect", detail={})
        session.add_all([stall, uncertain])
        await session.commit()
        stall_id, uncertain_id = stall.id, uncertain.id
    response = await request("POST", f'/api/workspace/tasks/{created["task_id"]}/{operation}')
    assert response.status_code == 200
    if operation == "resume":
        assert response.json()["controller_state"] == "ready"
        assert response.json()["retry_count"] == response.json()["transient_retry_count"] == 0
    async with pg_factory() as session:
        assert (await session.get(OwnerAttentionRow, stall_id)).resolved is True
        assert (await session.get(OwnerAttentionRow, uncertain_id)).resolved is False


async def test_completed_tasks_remain_immutable(pg_factory, monkeypatch):
    from atlas.persistence.models import TranscriptRow
    from atlas.transcript.repository import TranscriptRepository

    monkeypatch.setattr(tasks, "get_session_factory", lambda: pg_factory)
    created = await tasks.create_task({"objective": "Finished work", "acceptance_criteria": ["Verified"]})
    async with pg_factory() as session:
        row = await session.get(TranscriptRow, UUID(created["transcript_id"]))
        state = dict(row.active_task_state)
        state["status"] = "complete"
        await TranscriptRepository(session).update_active_task_state(row.id, state, expected_revision=row.active_task_revision)
        await session.commit()
    before = await tasks.get_task({"task_id": created["task_id"]})
    for operation in ("cancel", "resume"):
        assert (await request("POST", f'/api/workspace/tasks/{created["task_id"]}/{operation}')).status_code == 409
    assert await tasks.get_task({"task_id": created["task_id"]}) == before
    assert (await tasks.list_tasks({"status": "terminal"}))["items"][0]["task_id"] == created["task_id"]
