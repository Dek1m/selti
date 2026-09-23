"""Shared test fixtures for selti.

Исторический костыль (mock prometheus_client, вычищающий multiprocess_mode
из Counter/Histogram) удалён: metrics.py больше не передаёт этот параметр
в Counter/Histogram (валиден только для Gauge). Регрессию застеняет
test_metrics.py::test_module_imports_without_patches — прямой импорт
в чистом subprocess, без какого-либо патчинга.

circuitbreaker 2.1.3 doesn't have half_open_max_calls param.
We patch it.
"""

from unittest.mock import AsyncMock, MagicMock

# ── Patch CircuitBreaker to accept half_open_max_calls and add_state_change_listener ──
import circuitbreaker as _cb_mod

_orig_cb_init = _cb_mod.CircuitBreaker.__init__

def _patched_cb_init(self, *args, **kwargs):
    kwargs.pop("half_open_max_calls", None)
    self._state_change_listeners = []
    _orig_cb_init(self, *args, **kwargs)

_cb_mod.CircuitBreaker.__init__ = _patched_cb_init

# Add add_state_change_listener if missing
if not hasattr(_cb_mod.CircuitBreaker, "add_state_change_listener"):
    def _add_state_change_listener(self, listener):
        if not hasattr(self, "_state_change_listeners"):
            self._state_change_listeners = []
        self._state_change_listeners.append(listener)

    _cb_mod.CircuitBreaker.add_state_change_listener = _add_state_change_listener


import pytest

from datetime import datetime, timezone

from memory_server.config import Settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.memory.pg_repository import PostgreSQLRepository
from memory_server.memory.service import MemoryService


def memory_row(**overrides) -> dict:
    """Полная каноническая строка memories — мок asyncpg.Record из SQL-проекции.

    Соответствует _MEMORY_COLUMNS (db/queries.py): все новые колонки 018.
    """
    now = datetime.now(timezone.utc)
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "user_id": "u1",
        "content": "data",
        "metadata": {},
        "namespace": "default",
        "importance": 3,
        "created_at": now,
        "updated_at": now,
        "content_hash": None,
        "project_id": None,
        "status": "asserted",
        "confidence": 1.0,
        "valid_from": now,
        "valid_to": None,
        "ingested_at": now,
        "supersedes": None,
        "superseded_by": None,
        "frozen": False,
        "last_accessed_at": None,
        "access_count": 0,
    }
    row.update(overrides)
    return row


# ── Celery fixtures ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def celery_app():
    """Настроить Celery для тестов — task_always_eager=True.

    Выполняет задачи синхронно в том же процессе, без Redis/broker.
    """
    from memory_server.celery_app import app

    app.conf.update(task_always_eager=True)
    yield app
    app.conf.update(task_always_eager=False)


@pytest.fixture
def mock_pool():
    """Fixture that returns a mock asyncpg.Pool.

    Usage:
        async with mock_pool.acquire() as conn:
            conn.fetchrow(...)

    Важно: pool.acquire — MagicMock, а не AsyncMock.
    asyncpg.Pool.acquire() — корутина, возвращающая асинхронный контекстный менеджер.
    Используем MagicMock, чтобы `.acquire()` возвращал acm напрямую (без обёртки в корутину).
    """
    pool = MagicMock()
    conn = AsyncMock()

    # Асинхронный контекстный менеджер для acquire()
    acm = AsyncMock()
    acm.__aenter__.return_value = conn
    acm.__aexit__.return_value = None

    pool.acquire.return_value = acm

    # asyncpg.Connection.transaction() — обычный (не-корутиновый) метод,
    # возвращающий асинхронный контекстный менеджер. Нужен update()
    # (атомарность supersession, pg_repository.py → async with conn.transaction()).
    txn = AsyncMock()
    txn.__aenter__.return_value = conn
    txn.__aexit__.return_value = None
    conn.transaction = MagicMock(return_value=txn)

    return pool


@pytest.fixture
def mock_ns_resolver(mock_pool):
    """NamespaceRepository с мок-резолвом uid → фиксированный namespace_id UUID."""
    from memory_server.memory.namespace_repository import NamespaceRecord

    ns_repo = NamespaceRepository(pool=mock_pool)

    async def mock_get_by_uid(uid: str):
        rec = NamespaceRecord(
            id="00000000-0000-0000-0000-0000000000aa",
            uid=uid,
            name=uid,
            description="",
        )
        ns_repo._cache[uid] = rec
        return rec

    ns_repo.get_by_uid = mock_get_by_uid
    return ns_repo


@pytest.fixture
def mock_project_repository():
    """ProjectRepository-мок: резолв passthrough (slug/UUID → то же значение)."""
    from unittest.mock import AsyncMock

    project_repo = MagicMock()
    project_repo.resolve_id = AsyncMock(side_effect=lambda key: key)
    return project_repo


@pytest.fixture
def mock_repository(mock_pool, mock_ns_resolver):
    """Fixture that returns a MemoryRepository backed by a mock pool."""
    pg = PostgreSQLRepository(pool=mock_pool)
    repo = MemoryRepository(pg=pg, ns_repo=mock_ns_resolver)
    return repo


@pytest.fixture
def mock_embedding_provider():
    """Fixture that returns a mock embedding provider (EmbeddingProvider protocol)."""
    provider = MagicMock()
    provider.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    provider.embed_many = AsyncMock(return_value=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    return provider


@pytest.fixture
def mock_namespace_repository(mock_pool):
    """Fixture that returns a NamespaceRepository backed by a mock pool."""
    from unittest.mock import AsyncMock
    from memory_server.memory.namespace_repository import NamespaceRecord

    repo = NamespaceRepository(pool=mock_pool)
    # Pre-populate cache with default namespace for tests
    default_ns = NamespaceRecord(
        id="00000000-0000-0000-0000-000000000001",
        uid="default",
        name="Default",
        description="",
    )
    repo._cache["default"] = default_ns
    # Also mock get_or_create to return the default namespace for any uid
    async def mock_get_or_create(uid: str, name: str | None = None):
        if uid in repo._cache:
            return repo._cache[uid]
        # Auto-register with a deterministic ID
        import hashlib
        uid_hash = hashlib.md5(uid.encode()).hexdigest()[:12]
        ns_id = f"00000000-0000-0000-0000-{uid_hash}"
        rec = NamespaceRecord(
            id=ns_id,
            uid=uid,
            name=name or uid.replace("_", " ").title(),
            description="",
        )
        repo._cache[uid] = rec
        return rec

    repo.get_or_create = mock_get_or_create
    return repo


@pytest.fixture
def mock_service(mock_repository, mock_embedding_provider, mock_namespace_repository, mock_project_repository):
    """Fixture that returns a MemoryService with mocked deps.

    hybrid off: базовые тесты проверяют плотный путь; гибридный флоу —
    отдельные тесты с hybrid on (TestHybridSearch в test_service.py).
    """
    service = MemoryService(
        repository=mock_repository,
        embedding_provider=mock_embedding_provider,
        namespace_repository=mock_namespace_repository,
        runtime=RuntimeConfig(db_values={"dedup_enabled": False, "hybrid_search_enabled": False}),
        project_repository=mock_project_repository,
    )
    return service


@pytest.fixture
def dedup_engine(mock_repository, mock_embedding_provider):
    """Fixture that returns a DedupEngine with mocked deps and default config."""
    from memory_server.memory.dedup import DedupEngine

    return DedupEngine(
        repository=mock_repository,
        embedding_client=mock_embedding_provider,
        runtime=RuntimeConfig(db_values={}),
    )
