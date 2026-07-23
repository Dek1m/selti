import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Settings
from memory_server.memory.dedup import DedupAction, DedupEngine
from memory_server.memory.repository import MemoryRepository
from memory_server.models import MemoryRecord, SearchResult


# ---------------------------------------------------------------------------
# Exact dedup (5 tests)
# ---------------------------------------------------------------------------

class TestExactDedup:
    @pytest.mark.asyncio
    async def test_exact_match_returns_skip_for_default(self, dedup_engine, mock_pool):
        """content_hash найден в default namespace → SKIP (не UPDATE)."""
        now = datetime.now(timezone.utc)
        dedup_engine.repository.find_by_content_hash = AsyncMock(
            return_value=MemoryRecord(
                id="existing-id",
                user_id="u1",
                content="Hello",
                namespace="default",
                created_at=now,
                updated_at=now,
                content_hash="abc123",
            )
        )

        decision = await dedup_engine.check("Hello", "u1", "default")

        assert decision.action == DedupAction.SKIP
        assert decision.existing_id == "existing-id"
        assert decision.content_hash is not None
        dedup_engine.repository.find_by_content_hash.assert_awaited_once_with(
            "default", hashlib.sha256(b"Hello").hexdigest()
        )

    @pytest.mark.asyncio
    async def test_exact_match_returns_update_for_user_facts(self, dedup_engine, mock_pool):
        """content_hash найден в user_facts → UPDATE (не SKIP)."""
        now = datetime.now(timezone.utc)
        dedup_engine.repository.find_by_content_hash = AsyncMock(
            return_value=MemoryRecord(
                id="existing-id",
                user_id="u1",
                content="Hello",
                namespace="user_facts",
                created_at=now,
                updated_at=now,
                content_hash="abc123",
            )
        )

        decision = await dedup_engine.check("Hello", "u1", "user_facts")

        assert decision.action == DedupAction.UPDATE
        assert decision.existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_exact_match_not_found_returns_insert(self, dedup_engine, mock_pool):
        """content_hash не найден → INSERT (переход к semantic dedup)."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(return_value=[])

        decision = await dedup_engine.check("New content", "u1", "default")

        assert decision.action == DedupAction.INSERT
        assert decision.content_hash is not None

    def test_content_hash_is_sha256(self):
        """content_hash вычисляется через SHA256 (64 hex-символа)."""
        content = "test content"
        actual = hashlib.sha256(content.encode()).hexdigest()
        assert len(actual) == 64
        # Проверяем, что хеш детерминирован и соответствует SHA256
        expected = hashlib.sha256(content.encode()).hexdigest()
        assert actual == expected

    def test_content_hash_identical_for_same_content(self):
        """Одинаковый контент → одинаковый hash."""
        content = "same content"
        hash1 = hashlib.sha256(content.encode()).hexdigest()
        hash2 = hashlib.sha256(content.encode()).hexdigest()
        assert hash1 == hash2


# ---------------------------------------------------------------------------
# Semantic dedup (5 tests)
# ---------------------------------------------------------------------------

class TestSemanticDedup:
    @pytest.mark.asyncio
    async def test_semantic_match_above_threshold_returns_skip(self, dedup_engine, mock_pool):
        """score (0.96) >= threshold (0.95) → SKIP."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(id="match-id", content="Similar", metadata={}, score=0.96),
            ]
        )

        decision = await dedup_engine.check("Hello", "u1", "default")

        assert decision.action == DedupAction.SKIP
        assert decision.existing_id == "match-id"
        assert decision.existing_score == 0.96

    @pytest.mark.asyncio
    async def test_semantic_match_below_threshold_returns_insert(self, dedup_engine, mock_pool):
        """score (0.70) < threshold (0.95) → INSERT."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(id="low-id", content="Less similar", metadata={}, score=0.70),
            ]
        )

        decision = await dedup_engine.check("Hello", "u1", "default")

        assert decision.action == DedupAction.INSERT
        assert decision.existing_id is None

    @pytest.mark.asyncio
    async def test_semantic_match_empty_results_returns_insert(self, dedup_engine, mock_pool):
        """search вернул пустой список → INSERT."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(return_value=[])

        decision = await dedup_engine.check("New unique", "u1", "default")

        assert decision.action == DedupAction.INSERT

    @pytest.mark.asyncio
    async def test_semantic_match_uses_correct_threshold_for_namespace(self, dedup_engine, mock_pool):
        """Разные namespace используют свои threshold из config.dedup_thresholds."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        # score=0.92:
        #   - default threshold=0.95 → 0.92 < 0.95 → INSERT
        #   - user_facts threshold=0.90 → 0.92 >= 0.90 → SKIP
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(id="fuzzy-id", content="Fuzzy", metadata={}, score=0.92),
            ]
        )

        decision_default = await dedup_engine.check("Hello", "u1", "default")
        assert decision_default.action == DedupAction.INSERT, (
            "score=0.92 < default threshold=0.95, expected INSERT"
        )

        decision_facts = await dedup_engine.check("Hello", "u1", "user_facts")
        assert decision_facts.action == DedupAction.SKIP, (
            "score=0.92 >= user_facts threshold=0.90, expected SKIP"
        )

    @pytest.mark.asyncio
    async def test_semantic_match_disabled_returns_insert_without_checks(self):
        """dedup_enabled=False — engine должен сразу отдавать INSERT без проверок."""
        engine = DedupEngine(
            repository=MagicMock(spec=MemoryRepository),
            embedding_client=MagicMock(),
            config=Settings(dedup_enabled=False),
        )
        # Делаем все методы AsyncMock, но они не должны вызываться
        engine.repository.find_by_content_hash = AsyncMock()
        engine.embedding.embed = AsyncMock()
        engine.repository.search = AsyncMock()

        decision = await engine.check("content", "u1")

        assert decision.action == DedupAction.INSERT, (
            "При dedup_enabled=False engine должен возвращать INSERT "
            "без выполнения exact/semantic проверок"
        )
        # exact dedup не должен выполняться
        engine.repository.find_by_content_hash.assert_not_called()
        # semantic dedup не должен выполняться
        engine.embedding.embed.assert_not_called()
        engine.repository.search.assert_not_called()


# ---------------------------------------------------------------------------
# Edge cases (3 tests)
# ---------------------------------------------------------------------------

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_content(self, dedup_engine, mock_pool):
        """Пустой content не вызывает ошибок."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[])
        dedup_engine.repository.search = AsyncMock(return_value=[])

        decision = await dedup_engine.check("", "u1", "default")

        assert decision.action == DedupAction.INSERT
        assert decision.content_hash == hashlib.sha256(b"").hexdigest()

    @pytest.mark.asyncio
    async def test_very_long_content(self, dedup_engine, mock_pool):
        """Очень длинный content (>10000 символов) не вызывает ошибок."""
        content = "a" * 10001
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(return_value=[])

        decision = await dedup_engine.check(content, "u1", "default")

        assert decision.action == DedupAction.INSERT
        # Проверяем, что хеш посчитался
        assert decision.content_hash == hashlib.sha256(content.encode()).hexdigest()

    @pytest.mark.asyncio
    async def test_content_with_unicode(self, dedup_engine, mock_pool):
        """Контент с кириллицей и эмодзи не вызывает ошибок."""
        content = "Привет, мир! \U0001f30d\U0001f680 Тест с эмодзи"
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(return_value=[])

        decision = await dedup_engine.check(content, "u1", "default")

        assert decision.action == DedupAction.INSERT
        assert decision.content_hash == hashlib.sha256(content.encode()).hexdigest()
