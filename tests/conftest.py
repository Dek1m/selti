from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_server.memory.repository import MemoryRepository
from memory_server.memory.service import MemoryService


@pytest.fixture
def mock_pool():
    """Fixture that returns a mock asyncpg.Pool.

    Usage:
        async with mock_pool.acquire() as conn:
            conn.fetchrow(...)
    """
    pool = AsyncMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
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
def mock_service(mock_repository, mock_embedding_provider):
    """Fixture that returns a MemoryService with mocked deps."""
    service = MemoryService(
        repository=mock_repository,
        embedding_provider=mock_embedding_provider,
    )
    return service
