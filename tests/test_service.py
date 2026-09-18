import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from memory_server.config import Settings
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
        config=Settings(dedup_enabled=False, hybrid_search_enabled=False),
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
    async def test_store_update_branch_passes_content_and_hash(self, service):
        """user_facts UPDATE-ветка (Фаза 1.4): content + пересчитанный
        content_hash + embedding — иначе unique-индекс дедупа словит рассинхрон."""
        now = datetime.now(timezone.utc)
        existing = MemoryRecord(
            id="mem-existing", user_id="u1", content="old", metadata={"a": 1},
            namespace="user_facts", created_at=now, updated_at=now,
        )
        decision = DedupDecision(
            action=DedupAction.UPDATE,
            existing_id="mem-existing",
            content_hash=hashlib.sha256(b"new fact").hexdigest(),
            embedding=[0.9, 0.9, 0.9],
        )
        updated_record = existing.model_copy(update={"content": "new fact"})
        # dedup on: UPDATE-ветка достижима только через dedup.check
        service.config = Settings(dedup_enabled=True, hybrid_search_enabled=False)
        service.dedup.check = AsyncMock(return_value=decision)
        service.repository.get_by_id = AsyncMock(return_value=existing)
        service.repository.update = AsyncMock(return_value=updated_record)

        record, action = await service.store(
            content="new fact", user_id="u1", namespace="user_facts",
            metadata={"b": 2},
        )

        assert action == DedupAction.UPDATE
        assert record.content == "new fact"
        service.repository.update.assert_awaited_once_with(
            memory_id="mem-existing",
            content="new fact",
            content_hash=hashlib.sha256(b"new fact").hexdigest(),
            embedding=[0.9, 0.9, 0.9],
            metadata={"a": 1, "b": 2},  # dict-merge со старыми ключами
        )


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
        )
        assert result == results


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
    async def test_update_with_content_regenerates_embedding(self, service):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="mem-1",
            user_id="u1",
            content="updated",
            created_at=now,
            updated_at=now,
        )
        service.embedding.embed = AsyncMock(return_value=[0.9, 0.8, 0.7])
        service.repository.update = AsyncMock(return_value=record)

        result = await service.update(memory_id="mem-1", content="updated", metadata={"k": "v"})

        service.embedding.embed.assert_awaited_once_with("updated")
        service.repository.update.assert_awaited_once_with(
            memory_id="mem-1",
            content="updated",
            embedding=[0.9, 0.8, 0.7],
            metadata={"k": "v"},
            importance=None,
            project_id=None,
            supersedes=None,
            content_hash=hashlib.sha256(b"updated").hexdigest(),
        )
        assert result == record

    @pytest.mark.asyncio
    async def test_update_without_content_skips_embedding(self, service):
        now = datetime.now(timezone.utc)
        record = MemoryRecord(
            id="mem-1",
            user_id="u1",
            content="old",
            created_at=now,
            updated_at=now,
        )
        service.repository.update = AsyncMock(return_value=record)

        result = await service.update(memory_id="mem-1", metadata={"k": "v"})

        service.embedding.embed.assert_not_awaited()
        service.repository.update.assert_awaited_once_with(
            memory_id="mem-1",
            content=None,
            embedding=None,
            metadata={"k": "v"},
            importance=None,
            project_id=None,
            supersedes=None,
            content_hash=None,
        )
        assert result == record

    @pytest.mark.asyncio
    async def test_update_not_found_raises(self, service):
        service.repository.update = AsyncMock(return_value=None)

        with pytest.raises(NotFoundError) as exc_info:
            await service.update(memory_id="missing", content="x")
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

        service.repository.forget.assert_awaited_once_with(user_id="u1", namespace="ns")
        assert result == 7


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
        config=Settings(dedup_enabled=False, hybrid_search_enabled=True),
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
        service.config = Settings(traverse_max_nodes=5, hybrid_search_enabled=False)
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
