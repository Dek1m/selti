from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_server.config import Settings
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.memory.service import MemoryService


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
    return pool


@pytest.fixture
def mock_repository(mock_pool):
    """Fixture that returns a MemoryRepository backed by a mock pool."""
    repo = MemoryRepository(pool=mock_pool)
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
def mock_service(mock_repository, mock_embedding_provider, mock_namespace_repository):
    """Fixture that returns a MemoryService with mocked deps."""
    service = MemoryService(
        repository=mock_repository,
        embedding_provider=mock_embedding_provider,
        namespace_repository=mock_namespace_repository,
        config=Settings(dedup_enabled=False),
    )
    return service


@pytest.fixture
def dedup_engine(mock_repository, mock_embedding_provider):
    """Fixture that returns a DedupEngine with mocked deps and default config."""
    from memory_server.memory.dedup import DedupEngine

    return DedupEngine(
        repository=mock_repository,
        embedding_client=mock_embedding_provider,
        config=Settings(),
    )
