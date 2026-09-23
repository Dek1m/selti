import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from memory_server.config import Settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.exceptions import NotFoundError
from memory_server.memory.dedup import DedupAction, DedupDecision
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.service import MemoryService
from memory_server.models import MemoryListResult, MemoryRecord, SearchResult


@pytest.fixture
def service(mock_repository, mock_embedding_provider, mock_namespace_repository):
    # hybrid off: плотный путь; гибридный флоу — TestHybridSearch ниже
    return MemoryService(
        repository=mock_repository,
        embedding_provider=mock_embedding_provider,
        namespace_repository=mock_namespace_repository,
        runtime=RuntimeConfig(db_values={"dedup_enabled": False, "hybrid_search_enabled": False}),
    )


class TestStore:
    @pytest.mark.asyncio
    async def test_store_generates_embedding_and_returns_record(self, service):
        now = datetime.now(timezone.utc)
        expected_record = MemoryRecord(
            id="new-id",
            user_id="u1",
            content="Hello world",
            metadata={"source": "test"},
            namespace="ns1",
            created_at=now,
            updated_at=now,
        )

        service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        service.repository.insert = AsyncMock(return_value="new-id")
        service.repository.get_by_id = AsyncMock(return_value=expected_record)

        result, action = await service.store(
            content="Hello world",
            user_id="u1",
            metadata={"source": "test"},
            namespace="ns1",
        )

        assert action == DedupAction.INSERT
        service.embedding.embed.assert_awaited_once_with("Hello world")
        service.repository.insert.assert_awaited_once_with(
            user_id="u1",
            content="Hello world",
            embedding=[0.1, 0.2, 0.3],
            metadata={"source": "test"},
            namespace_id="00000000-0000-0000-0000-bfed25f845e5",
            content_hash=None,
            importance=3,
            project_id=None,
        )
        service.repository.get_by_id.assert_awaited_once_with("new-id")
        assert result == expected_record

    @pytest.mark.asyncio
    async def test_store_uses_default_metadata_and_namespace(self, service):
        now = datetime.now(timezone.utc)
        service.embedding.embed = AsyncMock(return_value=[0.0, 0.0, 0.0])
        service.repository.insert = AsyncMock(return_value="id-1")
        service.repository.get_by_id = AsyncMock(
            return_value=MemoryRecord(
                id="id-1",
                user_id="u1",
                content="x",
                created_at=now,
                updated_at=now,
            )
        )

        _, action = await service.store(content="x", user_id="u1")

        assert action == DedupAction.INSERT
        service.repository.insert.assert_awaited_once_with(
            user_id="u1",
            content="x",
            embedding=[0.0, 0.0, 0.0],
            metadata={},
            namespace_id="00000000-0000-0000-0000-000000000001",
            content_hash=None,
            importance=3,
            project_id=None,
        )

    @pytest.mark.asyncio
    async def test_store_raises_if_get_returns_none(self, service):
        service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        service.repository.insert = AsyncMock(return_value="ghost-id")
        service.repository.get_by_id = AsyncMock(return_value=None)

        with pytest.raises(RuntimeError, match="ghost-id"):
            await service.store(content="x", user_id="u1")

    @pytest.mark.asyncio
    async def test_store_exact_dup_confirms_without_content_rewrite(self, service):
        """user_facts exact-dup (V3.0, E.3 ADR-019): confirm-семантика.

        Контент не переписывается (история цела) — вместо этого metadata
        merge, confidence recovery c' = c + (1−c)×0.1 (cap 1.0), bump_access
        и sync links (Г3 ADR-017/019). Дубль по hash = контент совпал
        байт-в-байт, вектору и content_hash обновляться нечему.
        """
        now = datetime.now(timezone.utc)
        existing = MemoryRecord(
            id="mem-existing", user_id="u1", content="old", metadata={"a": 1},
            namespace="user_facts", created_at=now, updated_at=now,
            confidence=0.5,
        )
        decision = DedupDecision(
            action=DedupAction.UPDATE,
            existing_id="mem-existing",
            content_hash=hashlib.sha256(b"old").hexdigest(),
            embedding=[0.9, 0.9, 0.9],
        )
        confirmed = existing.model_copy(update={"confidence": 0.55})
        service.runtime = RuntimeConfig(db_values={"dedup_enabled": True, "hybrid_search_enabled": False})
        service.dedup.check = AsyncMock(return_value=decision)
        service.repository.get_by_id = AsyncMock(return_value=existing)
        service.repository.update = AsyncMock(return_value=confirmed)
        service.repository.bump_access = AsyncMock(return_value=1)
        service.repository.sync_links_to_relations = AsyncMock(return_value=0)

        record, action = await service.store(
            content="old", user_id="u1", namespace="user_facts",
            metadata={"b": 2, "links": [{"type": "related_to", "target": "x"}]},
        )

        assert action == DedupAction.UPDATE
        assert record.confidence == pytest.approx(0.55)
        # confirm: metadata merge + confidence recovery, БЕЗ content/embedding
        service.repository.update.assert_awaited_once_with(
            memory_id="mem-existing",
            metadata={"a": 1, "b": 2,
                      "links": [{"type": "related_to", "target": "x"}]},
            confidence=pytest.approx(0.5 + 0.5 * 0.1),
        )
        service.repository.bump_access.assert_awaited_once_with(["mem-existing"])
        # Г3: links синкаются и на confirm-пути
        service.repository.sync_links_to_relations.assert_awaited_once_with(
            "mem-existing"
        )

    @pytest.mark.asyncio
    async def test_store_confirm_confidence_caps_at_one(self, service):
        """confidence = 1.0 → recovery не превышает 1.0."""
        now = datetime.now(timezone.utc)
        existing = MemoryRecord(
            id="mem-existing", user_id="u1", content="c", metadata={},
            namespace="user_facts", created_at=now, updated_at=now,
            confidence=1.0,
        )
        decision = DedupDecision(
            action=DedupAction.UPDATE, existing_id="mem-existing",
            content_hash="h",
        )
        service.runtime = RuntimeConfig(db_values={"dedup_enabled": True, "hybrid_search_enabled": False})
        service.dedup.check = AsyncMock(return_value=decision)
        service.repository.get_by_id = AsyncMock(return_value=existing)
        service.repository.update = AsyncMock(
            return_value=existing.model_copy(update={"confidence": 1.0})
        )
        service.repository.bump_access = AsyncMock(return_value=1)

        await service.store(content="c", user_id="u1", namespace="user_facts")

        kwargs = service.repository.update.await_args.kwargs
        assert kwargs["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_store_confirm_sync_links_failure_non_fatal(self, service):
        """Сбой sync_links на confirm не роняет store (паттерн INSERT-пути)."""
        now = datetime.now(timezone.utc)
        existing = MemoryRecord(
            id="mem-existing", user_id="u1", content="c", metadata={},
            namespace="user_facts", created_at=now, updated_at=now,
            confidence=0.2,
        )
        decision = DedupDecision(
            action=DedupAction.UPDATE, existing_id="mem-existing", content_hash="h",
        )
        service.runtime = RuntimeConfig(db_values={"dedup_enabled": True, "hybrid_search_enabled": False})
        service.dedup.check = AsyncMock(return_value=decision)
        service.repository.get_by_id = AsyncMock(return_value=existing)
        service.repository.update = AsyncMock(return_value=existing)
        service.repository.bump_access = AsyncMock(return_value=1)
        service.repository.sync_links_to_relations = AsyncMock(
            side_effect=RuntimeError("db down")
        )

        record, action = await service.store(
            content="c", user_id="u1", namespace="user_facts",
            metadata={"links": [{"type": "related_to", "target": "x"}]},
        )

        assert action == DedupAction.UPDATE
        assert record.id == "mem-existing"

    @pytest.mark.asyncio
    async def test_store_semantic_skip_confirms_too(self, service):
        """SKIP-дубль (V3.0, E.4 ADR-019): semantic-совпадение — тот же
        confirm, что и exact-hash (В1 приёмки). Повтор факта другими
        словами тоже подтверждает его: confidence recovery + bump_access
        + sync links; контент и вектор не трогаются."""
        now = datetime.now(timezone.utc)
        existing = MemoryRecord(
            id="mem-existing", user_id="u1", content="сервер на 10.0.0.51",
            metadata={"a": 1}, namespace="code_knowledge",
            created_at=now, updated_at=now, confidence=0.4,
        )
        decision = DedupDecision(
            action=DedupAction.SKIP, existing_id="mem-existing",
            content_hash=hashlib.sha256("другая формулировка".encode()).hexdigest(),
            existing_score=0.91,
        )
        confirmed = existing.model_copy(update={"confidence": 0.46})
        service.runtime = RuntimeConfig(db_values={"dedup_enabled": True, "hybrid_search_enabled": False})
        service.dedup.check = AsyncMock(return_value=decision)
        service.repository.get_by_id = AsyncMock(return_value=existing)
        service.repository.update = AsyncMock(return_value=confirmed)
        service.repository.bump_access = AsyncMock(return_value=1)
        service.repository.sync_links_to_relations = AsyncMock(return_value=0)

        record, action = await service.store(
            content="ai-t-01: 10.0.0.51", user_id="u1", namespace="code_knowledge",
            metadata={"b": 2, "links": [{"type": "related_to", "target": "x"}]},
        )

        assert action == DedupAction.SKIP
        assert record.confidence == pytest.approx(0.46)
        service.repository.update.assert_awaited_once_with(
            memory_id="mem-existing",
            metadata={"a": 1, "b": 2,
                      "links": [{"type": "related_to", "target": "x"}]},
            confidence=pytest.approx(0.4 + 0.6 * 0.1),
        )
        service.repository.bump_access.assert_awaited_once_with(["mem-existing"])
        service.repository.sync_links_to_relations.assert_awaited_once_with(
            "mem-existing"
        )


class TestStoreManualPosition:
    """Ручные координаты 3D-карты memory_store(position) — миграция 025.

    Контракт: INSERT-путь сажает новую звезду; дедуп-пути (SKIP/UPDATE)
    двигают СУЩЕСТВУЮЩУЮ; без position поведение прежнее; сбой записи БД
    не роняет store (гранула дороже позиции); мусорный position — громко.
    """

    POSITION = {"x": 120.5, "y": -40.0, "z": 7}

    @pytest.mark.asyncio
    async def test_insert_path_places_new_star(self, service):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(id="mem-new", user_id="u1", content="x",
                              created_at=now, updated_at=now)
        service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        service.repository.insert = AsyncMock(return_value="mem-new")
        service.repository.get_by_id = AsyncMock(return_value=record)
        service.repository.map_layout_manual = AsyncMock(return_value=True)

        result, action = await service.store(
            content="x", user_id="u1", position=self.POSITION
        )

        assert action == DedupAction.INSERT and result.id == "mem-new"
        service.repository.map_layout_manual.assert_awaited_once_with(
            "mem-new", 120.5, -40.0, 7.0
        )

    @pytest.mark.asyncio
    async def test_exact_dup_moves_existing_star(self, service):
        """UPDATE-ветка (exact-hash): координаты едут на существующую гранулу —

        Мастер двигает звезду повторным store того же факта с position.
        """
        now = datetime.now(timezone.utc)
        existing = MemoryRecord(id="mem-old", user_id="u1", content="fact",
                                created_at=now, updated_at=now, confidence=0.5)
        decision = DedupDecision(
            action=DedupAction.UPDATE, existing_id="mem-old",
            content_hash=hashlib.sha256(b"fact").hexdigest(),
            embedding=[0.9, 0.9, 0.9],
        )
        service.runtime = RuntimeConfig(db_values={"dedup_enabled": True, "hybrid_search_enabled": False})
        service.dedup.check = AsyncMock(return_value=decision)
        service.repository.get_by_id = AsyncMock(return_value=existing)
        service.repository.update = AsyncMock(
            return_value=existing.model_copy(update={"confidence": 0.55})
        )
        service.repository.bump_access = AsyncMock(return_value=1)
        service.repository.map_layout_manual = AsyncMock(return_value=True)

        _, action = await service.store(
            content="fact", user_id="u1", position=self.POSITION
        )

        assert action == DedupAction.UPDATE
        service.repository.map_layout_manual.assert_awaited_once_with(
            "mem-old", 120.5, -40.0, 7.0
        )

    @pytest.mark.asyncio
    async def test_semantic_dup_moves_existing_star(self, service):
        """SKIP-ветка (semantic): тот же контракт — существующая звезда едет."""
        now = datetime.now(timezone.utc)
        existing = MemoryRecord(id="mem-sem", user_id="u1", content="факт",
                                created_at=now, updated_at=now, confidence=0.4)
        decision = DedupDecision(
            action=DedupAction.SKIP, existing_id="mem-sem",
            content_hash=hashlib.sha256("иначе".encode()).hexdigest(),
            existing_score=0.93,
        )
        service.runtime = RuntimeConfig(db_values={"dedup_enabled": True, "hybrid_search_enabled": False})
        service.dedup.check = AsyncMock(return_value=decision)
        service.repository.get_by_id = AsyncMock(return_value=existing)
        service.repository.update = AsyncMock(
            return_value=existing.model_copy(update={"confidence": 0.46})
        )
        service.repository.bump_access = AsyncMock(return_value=1)
        service.repository.map_layout_manual = AsyncMock(return_value=True)

        _, action = await service.store(
            content="факт по-другому", user_id="u1", position=self.POSITION
        )

        assert action == DedupAction.SKIP
        service.repository.map_layout_manual.assert_awaited_once_with(
            "mem-sem", 120.5, -40.0, 7.0
        )

    @pytest.mark.asyncio
    async def test_no_position_keeps_behavior(self, service):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(id="mem-plain", user_id="u1", content="x",
                              created_at=now, updated_at=now)
        service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        service.repository.insert = AsyncMock(return_value="mem-plain")
        service.repository.get_by_id = AsyncMock(return_value=record)
        service.repository.map_layout_manual = AsyncMock(return_value=True)

        await service.store(content="x", user_id="u1")

        service.repository.map_layout_manual.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_position_db_failure_non_fatal(self, service):
        """Слой БД упал (не валидация) — гранула сохранена, store не роняем."""
        now = datetime.now(timezone.utc)
        record = MemoryRecord(id="mem-db", user_id="u1", content="x",
                              created_at=now, updated_at=now)
        service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        service.repository.insert = AsyncMock(return_value="mem-db")
        service.repository.get_by_id = AsyncMock(return_value=record)
        service.repository.map_layout_manual = AsyncMock(
            side_effect=RuntimeError("pool exhausted")
        )

        result, action = await service.store(
            content="x", user_id="u1", position=self.POSITION
        )

        assert action == DedupAction.INSERT and result.id == "mem-db"

    @pytest.mark.asyncio
    async def test_position_garbage_raises_loud(self, service):
        """Мусорный position мимо тул-валидации — ValueError, не тихая потеря

        координат Мастера.
        """
        service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        with pytest.raises((ValueError, KeyError, TypeError)):
            await service.store(content="x", user_id="u1", position={"x": "abc"})


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_generates_query_embedding(self, service):
        results = [
            SearchResult(id="1", content="match", metadata={}, score=0.95),
        ]
        service.embedding.embed = AsyncMock(return_value=[0.5, 0.6, 0.7])
        service.repository.search = AsyncMock(return_value=results)

        result = await service.search(
            query="find this",
            user_id="u1",
            limit=5,
            threshold=0.8,
            namespace="ns",
        )

        service.embedding.embed.assert_awaited_once_with("find this")
        service.repository.search.assert_awaited_once_with(
            query_embedding=[0.5, 0.6, 0.7],
            user_id="u1",
            limit=5,
            threshold=0.8,
            namespace="ns",
            query_text="find this",
            project_id=None,
            include_historical=False,
            created_after=None,
            created_before=None,
            status=None,
        )
        assert result == results

    @pytest.mark.asyncio
    async def test_search_empty_result_increments_zero_result_metric(self, service):
        """Пустая выдача → zero_result_searches_total{namespace} +1 (Фаза 3.3)."""
        from memory_server.memory.service import ZERO_RESULT_SEARCHES_TOTAL

        service.embedding.embed = AsyncMock(return_value=[0.5])
        service.repository.search = AsyncMock(return_value=[])

        await service.search(query="nothing", namespace="ns")

        counter = ZERO_RESULT_SEARCHES_TOTAL.labels(namespace="ns")
        before = counter._value.get()
        await service.search(query="nothing", namespace="ns")
        assert counter._value.get() == before + 1

    @pytest.mark.asyncio
    async def test_search_no_namespace_counts_as_all(self, service):
        """Поиск без namespace (весь корпус) → label namespace='all'."""
        from memory_server.memory.service import ZERO_RESULT_SEARCHES_TOTAL

        service.embedding.embed = AsyncMock(return_value=[0.5])
        service.repository.search = AsyncMock(return_value=[])

        counter = ZERO_RESULT_SEARCHES_TOTAL.labels(namespace="all")
        before = counter._value.get()
        await service.search(query="everything")
        assert counter._value.get() == before + 1

    @pytest.mark.asyncio
    async def test_search_non_empty_does_not_increment_zero_result(self, service):
        """Непустая выдача метрику не трогает."""
        from memory_server.memory.service import ZERO_RESULT_SEARCHES_TOTAL

        service.embedding.embed = AsyncMock(return_value=[0.5])
        service.repository.search = AsyncMock(
            return_value=[SearchResult(id="1", content="m", metadata={}, score=0.9)]
        )

        counter = ZERO_RESULT_SEARCHES_TOTAL.labels(namespace="ns2")
        before = counter._value.get()
        await service.search(query="hit", namespace="ns2")
        assert counter._value.get() == before


class TestGet:
    @pytest.mark.asyncio
    async def test_get_returns_record(self, service):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="mem-1",
            user_id="u1",
            content="data",
            created_at=now,
            updated_at=now,
        )
        service.repository.get_by_id = AsyncMock(return_value=record)

        result = await service.get("mem-1")
        assert result == record
        service.repository.get_by_id.assert_awaited_once_with("mem-1")

    @pytest.mark.asyncio
    async def test_get_raises_not_found(self, service):
        service.repository.get_by_id = AsyncMock(return_value=None)

        with pytest.raises(NotFoundError) as exc_info:
            await service.get("missing-id")
        assert exc_info.value.id == "missing-id"


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_with_content_creates_version(self, service):
        """V3.0 (E.1 ADR-019): content = новая версия, не правка на месте.

        Сервис уходит в create_version: embedding, конфликт-чеки и закрытие
        старой — готовый путь Фазы 2; metadata/importance применяются к
        НОВОЙ версии; provenance supersede_reason='edit'.
        """
        now = datetime.now(timezone.utc)
        old = MemoryRecord(
            id="00000000-0000-0000-0000-000000000001", user_id="u1",
            content="old", metadata={"k": "v"}, namespace="default",
            created_at=now, updated_at=now,
        )
        new = MemoryRecord(
            id="00000000-0000-0000-0000-000000000002", user_id="u1",
            content="updated", supersedes="00000000-0000-0000-0000-000000000001",
            namespace="default", created_at=now, updated_at=now,
        )
        service.repository.get_by_id = AsyncMock(return_value=old)
        service.repository.find_by_content_hash = AsyncMock(return_value=None)
        service.repository.create_version = AsyncMock(return_value=new)
        # индикатор: правка на месте не должна происходить
        service.repository.update = AsyncMock(return_value=None)

        result = await service.update(
            memory_id="00000000-0000-0000-0000-000000000001",
            content="updated", importance=5,
        )

        assert result.id == "00000000-0000-0000-0000-000000000002"
        assert str(result.supersedes) == "00000000-0000-0000-0000-000000000001"
        service.embedding.embed.assert_awaited_once_with("updated")
        kwargs = service.repository.create_version.await_args.kwargs
        assert kwargs["old_id"] == "00000000-0000-0000-0000-000000000001"
        assert kwargs["content"] == "updated"
        assert kwargs["content_hash"] == hashlib.sha256(b"updated").hexdigest()
        assert kwargs["importance"] == 5
        # provenance правки + metadata тула применяются к новой версии
        # (поверх унаследованных старых ключей — dict-merge)
        assert kwargs["metadata"] == {"k": "v", "supersede_reason": "edit"}
        # правка на месте не происходила
        service.repository.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_with_content_identical_conflict(self, service):
        """content байт-в-байт равен текущему — create_version честно
        отказывает (переписывать нечего, подтверждение — store-путь)."""
        now = datetime.now(timezone.utc)
        old = MemoryRecord(
            id="00000000-0000-0000-0000-000000000001", user_id="u1",
            content="same", content_hash=hashlib.sha256(b"same").hexdigest(),
            namespace="default", created_at=now, updated_at=now,
        )
        service.repository.get_by_id = AsyncMock(return_value=old)

        from memory_server.exceptions import ConflictError

        with pytest.raises(ConflictError, match="identical"):
            await service.update(
                memory_id="00000000-0000-0000-0000-000000000001", content="same"
            )

    @pytest.mark.asyncio
    async def test_update_without_content_patches_wrapper(self, service):
        """Без content — правка обвязки на месте: metadata merge и т.д."""
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="mem-1", user_id="u1", content="old",
            created_at=now, updated_at=now,
        )
        service.repository.update = AsyncMock(return_value=record)

        result = await service.update(memory_id="mem-1", metadata={"k": "v"})

        service.embedding.embed.assert_not_awaited()
        service.repository.update.assert_awaited_once_with(
            memory_id="mem-1",
            metadata={"k": "v"},
            importance=None,
            project_id=None,
            supersedes=None,
            clear_project_id=False,
        )
        assert result == record

    @pytest.mark.asyncio
    async def test_update_not_found_raises(self, service):
        service.repository.update = AsyncMock(return_value=None)

        with pytest.raises(NotFoundError) as exc_info:
            await service.update(memory_id="missing", metadata={"x": 1})
        assert exc_info.value.id == "missing"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_delegates(self, service):
        service.repository.delete = AsyncMock(return_value=True)

        result = await service.delete("mem-1")
        assert result is True
        service.repository.delete.assert_awaited_once_with("mem-1")

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self, service):
        service.repository.delete = AsyncMock(return_value=False)

        result = await service.delete("mem-1")
        assert result is False


class TestList:
    @pytest.mark.asyncio
    async def test_list_delegates(self, service):
        now = datetime.now(timezone.utc)
        items = [
            MemoryRecord(id="1", user_id="u1", content="a", created_at=now, updated_at=now),
        ]
        expected = MemoryListResult(items=items, total=1)
        service.repository.list = AsyncMock(return_value=expected)

        result = await service.list(user_id="u1", namespace="ns", limit=10, offset=5)

        service.repository.list.assert_awaited_once_with(
            user_id="u1",
            namespace="ns",
            limit=10,
            offset=5,
            project_id=None,
        )
        assert result == expected

    @pytest.mark.asyncio
    async def test_list_defaults(self, service):
        service.repository.list = AsyncMock(return_value=MemoryListResult(items=[], total=0))

        await service.list()

        service.repository.list.assert_awaited_once_with(
            user_id=None,
            namespace=None,
            limit=50,
            offset=0,
            project_id=None,
        )


class TestForget:
    @pytest.mark.asyncio
    async def test_forget_delegates(self, service):
        service.repository.forget = AsyncMock(return_value=7)

        result = await service.forget(user_id="u1", namespace="ns")

        service.repository.forget.assert_awaited_once_with(
            user_id="u1", namespace="ns", project_id=None
        )
        assert result == 7

    @pytest.mark.asyncio
    async def test_forget_resolves_and_passes_project_id(self, service):
        """Фаза 3.1: slug проекта резолвится и уходит в repository."""
        service.resolve_project = AsyncMock(return_value="proj-uuid")
        service.repository.forget = AsyncMock(return_value=1)

        result = await service.forget(user_id="u1", project_id="akame")

        service.resolve_project.assert_awaited_once_with("akame")
        service.repository.forget.assert_awaited_once_with(
            user_id="u1", namespace=None, project_id="proj-uuid"
        )
        assert result == 1


# ---------------------------------------------------------------------------
# Hybrid search (Фаза 1.1/1.2): fusion → MMR → D4-ранжирование → bump_access
# ---------------------------------------------------------------------------


def _candidate(cid: str, *, namespace: str = "default", importance: int = 3,
               frozen: bool = False, age_days: float = 0.0,
               rank_dense: int | None = None, rank_fts: int | None = None,
               vector: list[float] | None = None) -> "HybridCandidate":
    """Кандидат с фиксированным «сейчас» — предсказуемый decay."""
    from memory_server.memory.search_fusion import HybridCandidate

    moment = datetime.now(timezone.utc) - timedelta(days=age_days)
    return HybridCandidate(
        id=cid, content=f"content-{cid}", metadata={}, namespace=namespace,
        importance=importance, project_id=None, status="asserted",
        created_at=moment, last_accessed_at=moment, frozen=frozen,
        rank_dense=rank_dense, rank_fts=rank_fts, vector=vector,
    )


@pytest.fixture
def hybrid_service(mock_repository, mock_embedding_provider, mock_namespace_repository):
    return MemoryService(
        repository=mock_repository,
        embedding_provider=mock_embedding_provider,
        namespace_repository=mock_namespace_repository,
        runtime=RuntimeConfig(db_values={"dedup_enabled": False, "hybrid_search_enabled": True}),
    )


class TestHybridSearch:
    @pytest.mark.asyncio
    async def test_hybrid_flow_fuses_and_bumps_access(self, hybrid_service):
        """Полный флоу: кандидаты → RRF → MMR → score>0 → батч-инкремент access."""
        candidates = [
            _candidate("both", rank_dense=0, rank_fts=0),   # пересечение каналов
            _candidate("dense-only", rank_dense=1),
            _candidate("fts-only", rank_fts=1),
        ]
        hybrid_service.embedding.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        hybrid_service.repository.search_hybrid = AsyncMock(return_value=candidates)
        hybrid_service.repository.bump_access = AsyncMock(return_value=3)

        results = await hybrid_service.search(query="q", user_id="u1", limit=2)

        assert len(results) == 2
        # Пересечение каналов получает сумму rrf-вкладов — выигрывает выдачу
        assert results[0].id == "both"
        assert all(r.score > 0 for r in results)
        # access-инкремент — по фактически выданным id
        hybrid_service.repository.bump_access.assert_awaited_once()
        bumped_ids = hybrid_service.repository.bump_access.await_args[0][0]
        assert sorted(bumped_ids) == sorted(r.id for r in results)

    @pytest.mark.asyncio
    async def test_hybrid_empty_candidates_returns_empty(self, hybrid_service):
        """Пустой сбор → [] и НИКАКОГО access-инкремента."""
        hybrid_service.embedding.embed = AsyncMock(return_value=[0.1])
        hybrid_service.repository.search_hybrid = AsyncMock(return_value=[])
        hybrid_service.repository.bump_access = AsyncMock()

        results = await hybrid_service.search(query="q")

        assert results == []
        hybrid_service.repository.bump_access.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hybrid_frozen_does_not_decay(self, hybrid_service):
        """Одинаковый rrf/вес: frozen со старой датой опережает не-frozen."""
        frozen_old = _candidate("frozen", frozen=True, age_days=365, rank_dense=0)
        fresh = _candidate("fresh", age_days=0, rank_dense=1)
        hybrid_service.embedding.embed = AsyncMock(return_value=[0.1])
        hybrid_service.repository.search_hybrid = AsyncMock(
            return_value=[frozen_old, fresh]
        )
        hybrid_service.repository.bump_access = AsyncMock()

        results = await hybrid_service.search(query="q", limit=2)

        assert results[0].id == "frozen"
        assert results[0].score > results[1].score

    @pytest.mark.asyncio
    async def test_hybrid_bump_failure_non_fatal(self, hybrid_service):
        """Сбой access-инкремента не роняет выдачу."""
        hybrid_service.embedding.embed = AsyncMock(return_value=[0.1])
        hybrid_service.repository.search_hybrid = AsyncMock(
            return_value=[_candidate("x", rank_dense=0)]
        )
        hybrid_service.repository.bump_access = AsyncMock(
            side_effect=RuntimeError("pool exhausted")
        )

        results = await hybrid_service.search(query="q")

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_hybrid_passes_include_historical(self, hybrid_service):
        """include_historical (time-travel, Фаза 1.3) прокидывается в сбор."""
        hybrid_service.embedding.embed = AsyncMock(return_value=[0.1])
        hybrid_service.repository.search_hybrid = AsyncMock(return_value=[])
        hybrid_service.repository.bump_access = AsyncMock()

        await hybrid_service.search(query="q", include_historical=True)

        hybrid_service.repository.search_hybrid.assert_awaited_once()
        kwargs = hybrid_service.repository.search_hybrid.await_args.kwargs
        assert kwargs["include_historical"] is True

    @pytest.mark.asyncio
    async def test_hybrid_score_decomposition_for_ui(self, hybrid_service):
        """Фаза 5.1: выдача несёт разложение score = rrf × decay × importance
        и мету карточки (namespace/даты/frozen) — ScoreGauge из WEB_UI_DESIGN §4.3."""
        candidates = [
            _candidate("c1", namespace="project_meta", importance=5,
                       age_days=1.0, rank_dense=0),
        ]
        hybrid_service.embedding.embed = AsyncMock(return_value=[0.1])
        hybrid_service.repository.search_hybrid = AsyncMock(return_value=candidates)
        hybrid_service.repository.bump_access = AsyncMock()

        results = await hybrid_service.search(query="q", limit=1)

        assert len(results) == 1
        top = results[0]
        assert top.namespace == "project_meta"
        assert top.frozen is False
        assert top.created_at is not None
        assert top.score_rrf is not None and top.score_rrf > 0
        assert top.score_decay is not None and 0 < top.score_decay <= 1
        # importance=5 → вес 5/3 (нейтраль 3) × множитель project_meta 1.1
        assert top.score_importance == pytest.approx(5 / 3 * 1.1, abs=1e-6)
        # Разложение сходится к итоговому score (round до 6 знаков)
        assert top.score == pytest.approx(
            top.score_rrf * top.score_decay * top.score_importance, abs=1e-5
        )


# ---------------------------------------------------------------------------
# Traverse caps (Фаза 1.5): cap traverse_max_nodes + курсорная пагинация
# ---------------------------------------------------------------------------


def _traverse_raw(n: int) -> dict:
    """raw-ответ хранимки: n узлов + рёбра цепочкой по соседям."""
    nodes = [{"id": f"n{i:04d}", "content": f"c{i}", "namespace": "default",
              "importance": 3, "depth": 0} for i in range(n)]
    edges = [
        {"id": f"e{i}", "source_id": f"n{i:04d}", "target_id": f"n{i+1:04d}",
         "link_type": "related_to", "description": None, "weight": 1.0,
         "metadata": {}}
        for i in range(n - 1)
    ]
    return {"nodes": nodes, "edges": edges}


class TestTraverseCaps:
    @pytest.fixture
    def capped_service(self, service):
        service.runtime = RuntimeConfig(db_values={"traverse_max_nodes": 5, "hybrid_search_enabled": False})
        return service

    @pytest.mark.asyncio
    async def test_cap_limits_nodes(self, capped_service):
        """Больше cap узлов — выдача урезана, truncated=True, total честный."""
        raw = _traverse_raw(8)
        now = datetime.now(timezone.utc)
        capped_service.repository.get_by_id = AsyncMock(
            return_value=MemoryRecord(id="start", user_id="u1", content="s",
                                      created_at=now, updated_at=now)
        )
        capped_service.repository.traverse = AsyncMock(return_value=raw)

        result = await capped_service.traverse("start")

        assert len(result.nodes) == 5
        assert result.total_nodes == 8
        assert result.truncated is True
        # узлы отсортированы по id — курсорная стабильность
        assert [n["id"] for n in result.nodes] == [f"n{i:04d}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_pagination_limit_offset(self, capped_service):
        """limit/offset — срез по отсортированным id (курсорная пагинация)."""
        raw = _traverse_raw(8)
        now = datetime.now(timezone.utc)
        capped_service.repository.get_by_id = AsyncMock(
            return_value=MemoryRecord(id="start", user_id="u1", content="s",
                                      created_at=now, updated_at=now)
        )
        capped_service.repository.traverse = AsyncMock(return_value=raw)

        page1 = await capped_service.traverse("start", limit=3, offset=0)
        page2 = await capped_service.traverse("start", limit=3, offset=3)

        assert [n["id"] for n in page1.nodes] == ["n0000", "n0001", "n0002"]
        # cap=5 урезал набор до n0000..n0004: offset=3 отдаёт хвост (2 узла)
        assert [n["id"] for n in page2.nodes] == ["n0003", "n0004"]
        assert page1.truncated is True

    @pytest.mark.asyncio
    async def test_edges_filtered_to_visible_nodes(self, capped_service):
        """Рёбра остаются только между выданными узлами (подграф страницы)."""
        raw = _traverse_raw(8)
        now = datetime.now(timezone.utc)
        capped_service.repository.get_by_id = AsyncMock(
            return_value=MemoryRecord(id="start", user_id="u1", content="s",
                                      created_at=now, updated_at=now)
        )
        capped_service.repository.traverse = AsyncMock(return_value=raw)

        result = await capped_service.traverse("start", limit=3)

        visible = {n["id"] for n in result.nodes}
        assert visible == {"n0000", "n0001", "n0002"}
        # цепочка внутри страницы: e0, e1; e2 (n0002→n0003) отсечён — цель вне страницы
        assert {e.id for e in result.edges} == {"e0", "e1"}
        assert all(e.source_id in visible for e in result.edges)

    @pytest.mark.asyncio
    async def test_under_cap_not_truncated(self, capped_service):
        raw = _traverse_raw(3)
        now = datetime.now(timezone.utc)
        capped_service.repository.get_by_id = AsyncMock(
            return_value=MemoryRecord(id="start", user_id="u1", content="s",
                                      created_at=now, updated_at=now)
        )
        capped_service.repository.traverse = AsyncMock(return_value=raw)

        result = await capped_service.traverse("start")

        assert len(result.nodes) == 3
        assert result.truncated is False
        assert result.total_nodes == 3
