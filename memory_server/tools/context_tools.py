"""MCP tools для «облачка знаний» (D9).

memory_context(project, refresh) — снапшот контекста проекта: Redis-кеш
ctx:{slug} (TTL = периоду beat) → таблица project_contexts (миграция 019).
refresh=true — немедленный пересчёт из топ-гранул (хранимка 020) + стек
(project_technologies/project_links, 017). Путь записи ставит dirty-флаг
ctx:{slug}:dirty — такой снапшот отдаётся с полем stale=true, beat
rebuild_contexts пересобирает грязные почасово (Фаза 6.1).
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
    материализованный снапшот из кеша/таблицы).

    Возвращает {project_id, content, sections, granule_count, computed_at, stale}:
    sections — {stack, decisions, code, insights, infra} (стек из
    project_technologies, топ-гранулы по namespace); stale=true — после
    снапшота были записи в проект (ждёт пересборки beat'ом).
    """
    return await celery_call(
        TASK_GET_CONTEXT,
        project=project,
        refresh=refresh,
    )
