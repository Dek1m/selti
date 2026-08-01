import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_server.config import Settings
from memory_server.memory.dedup import DedupAction, DedupEngine
from memory_server.memory.repository_qdrant import MemoryRepository
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


# ---------------------------------------------------------------------------
# Entity name dedup (6 tests)
# ---------------------------------------------------------------------------

class TestEntityNameDedup:
    @pytest.mark.asyncio
    async def test_same_entity_name_is_dedup(self, dedup_engine, mock_pool):
        """Обе гранулы с одинаковым entity_name → SKIP (как раньше)."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(
                    id="existing-id",
                    content="Similar content",
                    metadata={"entity_name": "adr-importance"},
                    score=0.96,
                ),
            ]
        )

        decision = await dedup_engine.check(
            "Similar content", "u1", "default",
            metadata={"entity_name": "adr-importance"},
        )

        assert decision.action == DedupAction.SKIP

    @pytest.mark.asyncio
    async def test_different_entity_name_is_not_dedup(self, dedup_engine, mock_pool):
        """Разные entity_name → INSERT (не дубль)."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(
                    id="existing-id",
                    content="Similar content",
                    metadata={"entity_name": "adr-importance"},
                    score=0.96,
                ),
            ]
        )

        decision = await dedup_engine.check(
            "Similar content", "u1", "default",
            metadata={"entity_name": "rb-01-audit"},
        )

        assert decision.action == DedupAction.INSERT

    @pytest.mark.asyncio
    async def test_one_missing_entity_name_is_dedup(self, dedup_engine, mock_pool):
        """У одной нет entity_name → SKIP (backward compat)."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(
                    id="existing-id",
                    content="Similar content",
                    metadata={},
                    score=0.96,
                ),
            ]
        )

        decision = await dedup_engine.check(
            "Similar content", "u1", "default",
            metadata={"entity_name": "new-entity"},
        )

        assert decision.action == DedupAction.SKIP

    @pytest.mark.asyncio
    async def test_both_missing_entity_name_is_dedup(self, dedup_engine, mock_pool):
        """У обеих нет entity_name → SKIP (как раньше)."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(
                    id="existing-id",
                    content="Similar content",
                    metadata={},
                    score=0.96,
                ),
            ]
        )

        decision = await dedup_engine.check(
            "Similar content", "u1", "default",
            metadata={},
        )

        assert decision.action == DedupAction.SKIP

    @pytest.mark.asyncio
    async def test_different_entity_name_case_insensitive(self, dedup_engine, mock_pool):
        """Разный регистр entity_name → SKIP (нормализация работает)."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(
                    id="existing-id",
                    content="Similar content",
                    metadata={"entity_name": "DedupEngine"},
                    score=0.96,
                ),
            ]
        )

        decision = await dedup_engine.check(
            "Similar content", "u1", "default",
            metadata={"entity_name": "dedupengine"},
        )

        assert decision.action == DedupAction.SKIP

    @pytest.mark.asyncio
    async def test_different_entity_name_with_whitespace(self, dedup_engine, mock_pool):
        """Пробелы в entity_name → SKIP (нормализация работает)."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        dedup_engine.repository.search = AsyncMock(
            return_value=[
                SearchResult(
                    id="existing-id",
                    content="Similar content",
                    metadata={"entity_name": "  DedupEngine  "},
                    score=0.96,
                ),
            ]
        )

        decision = await dedup_engine.check(
            "Similar content", "u1", "default",
            metadata={"entity_name": "DedupEngine"},
        )

        assert decision.action == DedupAction.SKIP


# ---------------------------------------------------------------------------
# Batch dedup (6 tests)
# ---------------------------------------------------------------------------

class TestBatchDedup:
    @pytest.mark.asyncio
    async def test_batch_all_insert(self, dedup_engine, mock_pool):
        """Все записи новые → все INSERT, embed_many вызван один раз."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed_many = AsyncMock(
            return_value=[[0.1], [0.2], [0.3]]
        )
        dedup_engine.repository.search = AsyncMock(return_value=[])

        entries = [
            {"content": "A", "namespace": "default"},
            {"content": "B", "namespace": "default"},
            {"content": "C", "namespace": "default"},
        ]
        decisions = await dedup_engine.check_batch(entries, "u1")

        assert len(decisions) == 3
        assert all(d.action == DedupAction.INSERT for d in decisions)
        # embed_many вызван ровно 1 раз с 3 текстами
        dedup_engine.embedding.embed_many.assert_awaited_once()
        call_args = dedup_engine.embedding.embed_many.call_args[0][0]
        assert call_args == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_batch_exact_dedup(self, dedup_engine, mock_pool):
        """2 из 3 — exact match → SKIP, embed_many только для 1 нового."""
        now = datetime.now(timezone.utc)

        async def mock_find(ns, h):
            if h == hashlib.sha256(b"A").hexdigest():
                return MemoryRecord(id="id-a", user_id="u1", content="A",
                                    namespace=ns, created_at=now, updated_at=now, content_hash=h)
            if h == hashlib.sha256(b"C").hexdigest():
                return MemoryRecord(id="id-c", user_id="u1", content="C",
                                    namespace=ns, created_at=now, updated_at=now, content_hash=h)
            return None

        dedup_engine.repository.find_by_content_hash = AsyncMock(side_effect=mock_find)
        dedup_engine.embedding.embed_many = AsyncMock(return_value=[[0.5]])
        dedup_engine.repository.search = AsyncMock(return_value=[])

        entries = [
            {"content": "A", "namespace": "default"},
            {"content": "B", "namespace": "default"},
            {"content": "C", "namespace": "default"},
        ]
        decisions = await dedup_engine.check_batch(entries, "u1")

        assert decisions[0].action == DedupAction.SKIP
        assert decisions[0].existing_id == "id-a"
        assert decisions[1].action == DedupAction.INSERT
        assert decisions[2].action == DedupAction.SKIP
        assert decisions[2].existing_id == "id-c"
        # embed_many вызван только для B
        dedup_engine.embedding.embed_many.assert_awaited_once()
        call_args = dedup_engine.embedding.embed_many.call_args[0][0]
        assert call_args == ["B"]

    @pytest.mark.asyncio
    async def test_batch_semantic_dedup(self, dedup_engine, mock_pool):
        """Semantic dedup: 1 из 2 — score >= threshold → SKIP, другой — INSERT."""
        dedup_engine.repository.find_by_content_hash = AsyncMock(return_value=None)
        dedup_engine.embedding.embed_many = AsyncMock(
            return_value=[[0.1, 0.2], [0.3, 0.4]]
        )
        # Первый вызов — match, второй — нет
        search_results = [
            [SearchResult(id="semantic-id", content="Similar", metadata={}, score=0.97)],
            [],
        ]
        call_count = 0
        async def mock_search(**kwargs):
            nonlocal call_count
            result = search_results[call_count]
            call_count += 1
            return result

        dedup_engine.repository.search = AsyncMock(side_effect=mock_search)

        entries = [
            {"content": "X", "namespace": "default"},
            {"content": "Y", "namespace": "default"},
        ]
        decisions = await dedup_engine.check_batch(entries, "u1")

        assert decisions[0].action == DedupAction.SKIP
        assert decisions[0].existing_id == "semantic-id"
        assert decisions[1].action == DedupAction.INSERT

    @pytest.mark.asyncio
    async def test_batch_disabled_dedup(self):
        """dedup_enabled=False → все INSERT без проверок."""
        from memory_server.memory.dedup import DedupEngine
        engine = DedupEngine(
            repository=MagicMock(spec=MemoryRepository),
            embedding_client=MagicMock(),
            config=Settings(dedup_enabled=False),
        )
        engine.repository.find_by_content_hash = AsyncMock()
        engine.embedding.embed_many = AsyncMock()
        engine.repository.search = AsyncMock()

        entries = [
            {"content": "A", "namespace": "default"},
            {"content": "B", "namespace": "default"},
        ]
        decisions = await engine.check_batch(entries, "u1")

        assert all(d.action == DedupAction.INSERT for d in decisions)
        engine.repository.find_by_content_hash.assert_not_called()
        engine.embedding.embed_many.assert_not_called()
        engine.repository.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_empty(self, dedup_engine, mock_pool):
        """Пустой список → пустой результат."""
        decisions = await dedup_engine.check_batch([], "u1")
        assert decisions == []

    @pytest.mark.asyncio
    async def test_batch_user_facts_update(self, dedup_engine, mock_pool):
        """Exact match в user_facts → UPDATE (не SKIP)."""
        now = datetime.now(timezone.utc)

        async def mock_find(ns, h):
            if ns == "user_facts" and h == hashlib.sha256(b"A").hexdigest():
                return MemoryRecord(id="id-a", user_id="u1", content="A",
                                    namespace="user_facts", created_at=now,
                                    updated_at=now, content_hash=h)
            return None

        dedup_engine.repository.find_by_content_hash = AsyncMock(side_effect=mock_find)
        dedup_engine.embedding.embed_many = AsyncMock(return_value=[[0.1]])
        dedup_engine.repository.search = AsyncMock(return_value=[])

        entries = [
            {"content": "A", "namespace": "user_facts"},
            {"content": "B", "namespace": "user_facts"},
        ]
        decisions = await dedup_engine.check_batch(entries, "u1")

        assert decisions[0].action == DedupAction.UPDATE
        assert decisions[0].existing_id == "id-a"
        assert decisions[1].action == DedupAction.INSERT
