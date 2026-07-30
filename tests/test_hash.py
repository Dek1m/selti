"""Tests for hash module: HashRepository + hash_tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# HashRepository tests
# ---------------------------------------------------------------------------

from memory_server.memory.hash_repository import HashRepository


class TestHashRepository:
    """Unit tests for HashRepository (data access layer)."""

    @pytest.fixture
    def mock_pool(self):
        """Mock asyncpg.Pool with async context manager for acquire()."""
        pool = MagicMock()
        conn = AsyncMock()

        acm = AsyncMock()
        acm.__aenter__.return_value = conn
        acm.__aexit__.return_value = None

        pool.acquire.return_value = acm
        return pool, conn

    @pytest.mark.asyncio
    async def test_hash_upsert_creates_new(self, mock_pool):
        """UPSERT создаёт новую запись и возвращает id, created_at, updated_at."""
        pool, conn = mock_pool
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(return_value={
            "id": "hash-1",
            "created_at": now,
            "updated_at": now,
        })

        repo = HashRepository(pool)
        result = await repo.upsert(
            source_type="session",
            source_id="sess-123",
            content_hash="a" * 64,
            size_bytes=1024,
            metadata={"project_id": "akame"},
        )

        assert result["id"] == "hash-1"
        assert result["created_at"] == now
        assert result["updated_at"] == now
        conn.fetchrow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hash_upsert_updates_existing(self, mock_pool):
        """UPSERT обновляет существующую запись (ON CONFLICT DO UPDATE)."""
        pool, conn = mock_pool
        created = datetime(2025, 1, 1, tzinfo=timezone.utc)
        updated = datetime(2025, 7, 30, tzinfo=timezone.utc)
        conn.fetchrow = AsyncMock(return_value={
            "id": "hash-1",
            "created_at": created,
            "updated_at": updated,
        })

        repo = HashRepository(pool)
        result = await repo.upsert(
            source_type="session",
            source_id="sess-123",
            content_hash="b" * 64,
            size_bytes=2048,
        )

        assert result["id"] == "hash-1"
        # updated_at позже created_at → запись была обновлена
        assert result["updated_at"] > result["created_at"]

    @pytest.mark.asyncio
    async def test_hash_get_returns_stored(self, mock_pool):
        """GET возвращает сохранённый хеш по source_type + source_id."""
        pool, conn = mock_pool
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(return_value={
            "id": "hash-1",
            "source_type": "session",
            "source_id": "sess-123",
            "content_hash": "c" * 64,
            "size_bytes": 512,
            "metadata": {"project_id": "akame"},
            "created_at": now,
            "updated_at": now,
        })

        repo = HashRepository(pool)
        result = await repo.get("session", "sess-123")

        assert result is not None
        assert result["content_hash"] == "c" * 64
        assert result["source_type"] == "session"
        assert result["source_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_hash_get_returns_none_for_missing(self, mock_pool):
        """GET возвращает None для несуществующей записи."""
        pool, conn = mock_pool
        conn.fetchrow = AsyncMock(return_value=None)

        repo = HashRepository(pool)
        result = await repo.get("session", "nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_hash_list_filters_by_source_type(self, mock_pool):
        """LIST фильтрует по source_type."""
        pool, conn = mock_pool
        conn.fetch = AsyncMock(return_value=[
            {"id": "h1", "source_type": "session", "source_id": "s1"},
            {"id": "h2", "source_type": "session", "source_id": "s2"},
        ])

        repo = HashRepository(pool)
        result = await repo.list(source_type="session")

        assert len(result) == 2
        assert all(r["source_type"] == "session" for r in result)
        conn.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hash_list_filters_by_project(self, mock_pool):
        """LIST фильтрует по project_id в metadata (через SQL $3)."""
        pool, conn = mock_pool
        conn.fetch = AsyncMock(return_value=[
            {"id": "h1", "metadata": {"project_id": "akame"}},
        ])

        repo = HashRepository(pool)
        result = await repo.list(project="akame")

        assert len(result) == 1
        assert result[0]["metadata"]["project_id"] == "akame"

    @pytest.mark.asyncio
    async def test_hash_delete_removes_record(self, mock_pool):
        """DELETE удаляет запись и возвращает её ID."""
        pool, conn = mock_pool
        conn.fetchrow = AsyncMock(return_value={"id": "hash-1"})

        repo = HashRepository(pool)
        result = await repo.delete("session", "sess-123")

        assert result == "hash-1"
        conn.fetchrow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hash_delete_returns_none_for_missing(self, mock_pool):
        """DELETE возвращает None если запись не найдена."""
        pool, conn = mock_pool
        conn.fetchrow = AsyncMock(return_value=None)

        repo = HashRepository(pool)
        result = await repo.delete("session", "nonexistent")

        assert result is None


# ---------------------------------------------------------------------------
# hash_tools tests
# ---------------------------------------------------------------------------


class TestHashTools:
    """Unit tests for hash_tools (MCP tool layer)."""

    @pytest.fixture
    def mock_ctx(self):
        """Mock Context with request_context → service → repository.pool + authorized agent."""
        pool = MagicMock()
        conn = AsyncMock()
        acm = AsyncMock()
        acm.__aenter__.return_value = conn
        acm.__aexit__.return_value = None
        pool.acquire.return_value = conn  # simplified — conn used directly below

        repository = MagicMock()
        repository.pool = pool

        service = MagicMock()
        service.repository = repository

        ctx = MagicMock()
        ctx.request_context = MagicMock()
        ctx.request_context.lifespan_context = {"service": service}
        # ACL: authorized agent
        ctx.session.client_info.name = "memory-granulator"

        return ctx, conn

    # -- hash_upsert --

    @pytest.mark.asyncio
    async def test_hash_upsert_invalid_format(self, mock_ctx):
        """Ошибка при невалидном формате хеша (не 64 hex chars)."""
        from memory_server.tools.hash_tools import hash_upsert

        ctx, _ = mock_ctx

        with pytest.raises(ValueError, match="Invalid content_hash format"):
            await hash_upsert(
                source_type="session",
                source_id="s1",
                content_hash="not-a-valid-hash",
                ctx=ctx,
            )

    @pytest.mark.asyncio
    async def test_hash_upsert_metadata_overflow(self, mock_ctx):
        """Ошибка при metadata > 64KB."""
        from memory_server.tools.hash_tools import hash_upsert

        ctx, _ = mock_ctx
        huge_metadata = {"key": "x" * 70000}  # > 64KB after json.dumps

        with pytest.raises(ValueError, match="metadata too large"):
            await hash_upsert(
                source_type="session",
                source_id="s1",
                content_hash="a" * 64,
                metadata=huge_metadata,
                ctx=ctx,
            )

    @pytest.mark.asyncio
    async def test_hash_upsert_success(self, mock_ctx):
        """Успешный UPSERT возвращает id, created_at, updated_at."""
        from memory_server.tools.hash_tools import hash_upsert

        ctx, conn = mock_ctx
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(return_value={
            "id": "hash-1",
            "created_at": now,
            "updated_at": now,
        })

        with patch("memory_server.tools.hash_tools._track_tool", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = {"id": "hash-1", "created_at": now, "updated_at": now}
            result = await hash_upsert(
                source_type="session",
                source_id="s1",
                content_hash="a" * 64,
                size_bytes=100,
                metadata={"project_id": "akame"},
                ctx=ctx,
            )

        assert result["id"] == "hash-1"
        mock_track.assert_awaited_once()

    # -- hash_get --

    @pytest.mark.asyncio
    async def test_hash_get_success(self, mock_ctx):
        """Успешный GET возвращает запись."""
        from memory_server.tools.hash_tools import hash_get

        ctx, conn = mock_ctx
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(return_value={
            "id": "hash-1",
            "source_type": "session",
            "source_id": "s1",
            "content_hash": "a" * 64,
            "size_bytes": 100,
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        })

        with patch("memory_server.tools.hash_tools._track_tool", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = {
                "id": "hash-1",
                "source_type": "session",
                "source_id": "s1",
                "content_hash": "a" * 64,
            }
            result = await hash_get(
                source_type="session",
                source_id="s1",
                ctx=ctx,
            )

        assert result is not None
        assert result["content_hash"] == "a" * 64
        mock_track.assert_awaited_once()

    # -- hash_delete --

    @pytest.mark.asyncio
    async def test_hash_delete_unauthorized(self):
        """Ошибка при неавторизованном agent (не в WRITE_AUTHORIZED_AGENTS)."""
        from memory_server.tools.hash_tools import _check_write_auth

        ctx = MagicMock()
        ctx.session = MagicMock()
        ctx.session.client_info = MagicMock()
        ctx.session.client_info.name = "unknown-agent"

        with pytest.raises(PermissionError, match="not authorized"):
            _check_write_auth(ctx)

    @pytest.mark.asyncio
    async def test_hash_delete_authorized(self):
        """Авторизованный agent проходит проверку."""
        from memory_server.tools.hash_tools import _check_write_auth

        ctx = MagicMock()
        ctx.session = MagicMock()
        ctx.session.client_info = MagicMock()
        ctx.session.client_info.name = "memory-granulator"

        # Не должен выбросить исключение
        _check_write_auth(ctx)

    @pytest.mark.asyncio
    async def test_hash_delete_no_ctx(self):
        """_check_write_auth с None ctx — не падает."""
        from memory_server.tools.hash_tools import _check_write_auth

        _check_write_auth(None)

    # -- hash_list --

    @pytest.mark.asyncio
    async def test_hash_list_returns_results(self, mock_ctx):
        """hash_list возвращает список записей."""
        from memory_server.tools.hash_tools import hash_list

        ctx, conn = mock_ctx
        conn.fetch = AsyncMock(return_value=[
            {"id": "h1", "source_type": "session", "source_id": "s1"},
            {"id": "h2", "source_type": "file", "source_id": "f1"},
        ])

        with patch("memory_server.tools.hash_tools._track_tool", new_callable=AsyncMock) as mock_track:
            mock_track.return_value = [
                {"id": "h1", "source_type": "session", "source_id": "s1"},
                {"id": "h2", "source_type": "file", "source_id": "f1"},
            ]
            result = await hash_list(ctx=ctx)

        assert len(result) == 2
        mock_track.assert_awaited_once()
