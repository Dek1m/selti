"""Lifespan MCP-сервера: старт при недоступном PG (приёмка Фазы 3).

Философия деградации: мёртвый PG на старте — WARNING + degraded-режим,
не crash-loop uvicorn («Application startup failed»). Схема уже
деградирует graceful (SchemaPendingError в pg_repository — покрыто
test_lifecycle.py); старт выровнен с этой философией.
"""

from unittest.mock import patch

import pytest

from memory_server import server as server_module


async def _dead_pg_migrations():
    """Миграции при мёртвом PG: connection refused."""
    raise ConnectionError("connection refused (dead PG)")


class TestLifespanDegradedStart:
    @pytest.mark.asyncio
    async def test_migrations_failure_starts_degraded(self, caplog):
        """Ошибка миграций → lifespan входит в yield, WARNING в логе.

        До фикса: исключение из run_migrations роняло lifespan →
        uvicorn «Application startup failed» → crash-loop рестартов.
        """
        with patch.object(
            server_module, "run_migrations", new=_dead_pg_migrations
        ), caplog.at_level("WARNING", logger=server_module.__name__):
            async with server_module.lifespan(server_module.mcp):
                # yield прошёл — приложение стартовало
                warnings = [
                    r for r in caplog.records
                    if r.levelname == "WARNING"
                    and "starting degraded" in r.getMessage()
                ]
                assert warnings, (
                    "ожидается WARNING 'migrations pending, starting degraded'"
                )
                # Причина деградации — в structured extra, не в message
                assert "dead PG" in warnings[0].error

    @pytest.mark.asyncio
    async def test_migrations_success_no_degraded_warning(self, caplog):
        """Успешные миграции → никакого WARNING про degraded."""
        async def _ok_migrations():
            return None

        with patch.object(
            server_module, "run_migrations", new=_ok_migrations
        ), caplog.at_level("WARNING", logger=server_module.__name__):
            async with server_module.lifespan(server_module.mcp):
                assert not [
                    r for r in caplog.records
                    if "starting degraded" in r.getMessage()
                ]
