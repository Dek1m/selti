"""MCP tools для «облачка знаний» (D9).

memory_context(project, refresh) — снапшот контекста проекта из таблицы
project_contexts (миграция 019): секции по namespace, собранные из топ-гранул
проекта хранимкой project_context_snapshot. Всё через Celery (принцип 3);
Redis-кеш ctx:{slug} и SessionStart-хук ZCode — Фаза 6.
"""

from typing import Any

from fastmcp import Context

from memory_server.server import mcp
from memory_server.tools.task_bridge import celery_call
from memory_server.utils.metrics_decorator import tool_handler

TASK_GET_CONTEXT = "memory_server.tasks.context_tasks.get_project_context"


@mcp.tool()
@tool_handler("memory_context")
async def memory_context(
    project: str,
    refresh: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Получить снапшот контекста проекта («облачко знаний»).

    project: slug (например 'selti') или UUID проекта.
    refresh: true — немедленный пересчёт из топ-гранул (по умолчанию отдаётся
    материализованный снапшот).

    Возвращает {project_id, content, sections, granule_count, computed_at}:
    sections — топ-гранулы по namespace (решения, код, инсайты, инфраструктура).
    """
    return await celery_call(
        TASK_GET_CONTEXT,
        project=project,
        refresh=refresh,
    )
