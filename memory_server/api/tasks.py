"""REST endpoints для управления Celery задачами.

Предоставляет API для мониторинга и управления задачами:
- GET /tasks — список активных задач
- GET /tasks/{task_id} — статус конкретной задачи
- POST /tasks/{task_id}/cancel — отмена задачи
"""

import asyncio
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/")
async def list_active_tasks():
    """Список активных задач на всех workers."""
    from memory_server.celery_app import app

    def _inspect():
        inspect = app.control.inspect(timeout=5)
        active = inspect.active() or {}
        return {"active": active}

    return await asyncio.to_thread(_inspect)


@router.get("/{task_id}")
async def get_task_status(task_id: str):
    """Получить статус задачи по ID."""
    from memory_server.celery_app import app

    def _check():
        result = app.AsyncResult(task_id)
        return {
            "task_id": task_id,
            "status": result.status,
            "result": result.result if result.ready() else None,
        }

    return await asyncio.to_thread(_check)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Отменить задачу."""
    from memory_server.celery_app import app

    def _revoke():
        app.control.revoke(task_id, terminate=True)
        return {"task_id": task_id, "status": "cancelled"}

    return await asyncio.to_thread(_revoke)