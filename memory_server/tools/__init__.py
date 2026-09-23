"""Реестр MCP-тулов по доменным модулям.

Каждый элемент — модуль, регистрирующий тулы декоратором @mcp.tool()
(срабатывает на импорте). server.py подключает тулы ТОЛЬКО через этот
реестр — единая точка входа. Нарезка по образцу mia (Приложение A):
модуль = будущий ModuleBase при потенциальном переезде под mia.
"""

TOOL_MODULES: tuple[str, ...] = (
    "memory_server.tools.memory_tools",
    "memory_server.tools.hash_tools",
    # TODO(Фаза 3/6): «облачко знаний» — memory_context, снапшоты project_contexts
    "memory_server.tools.context_tools",
    # TODO(Фаза 0/3): реестр проектов — CRUD, семантический поиск по Qdrant-коллекции
    "memory_server.tools.project_tools",
    # System One: рефлекс-слой Open-Jev-2B (прямой HTTP, без Celery — stateless)
    "memory_server.tools.jev_tools",
)
