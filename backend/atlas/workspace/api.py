"""Owner dashboard routes, protected by the application's global /api boundary."""

from collections.abc import Awaitable
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from atlas.workspace import tasks

router = APIRouter(prefix="/api/workspace/tasks", tags=["workspace"])


async def _result(operation: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await operation
    except tasks.TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except tasks.TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
async def list_tasks(
    status: Literal["all", "active", "terminal"] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    return await _result(tasks.list_tasks({"status": status, "limit": limit, "offset": offset}))


@router.get("/{task_id}")
async def get_task(task_id: UUID) -> dict[str, Any]:
    return await _result(tasks.get_task({"task_id": str(task_id)}))


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: UUID) -> dict[str, Any]:
    return await _result(tasks.cancel_task({"task_id": str(task_id)}))


@router.post("/{task_id}/resume")
async def resume_task(task_id: UUID) -> dict[str, Any]:
    return await _result(tasks.resume_task({"task_id": str(task_id)}))
