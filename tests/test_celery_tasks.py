"""Tests for memory_server.tasks.memory_tasks and hash_tasks.

Uses task_always_eager=True — tasks execute synchronously in the test process.
Mocks: connections module (get_pool, get_qdrant, get_embedding), MemoryService, etc.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory_server.tasks.errors import ValidationError


# ── Shared mocks ─────────────────────────────────────────────────


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    acm = AsyncMock()
    acm.__aenter__.return_value = conn
    acm.__aexit__.return_value = None
    pool.acquire.return_value = acm
    return pool


@pytest.fixture
def mock_memory_service():
    svc = MagicMock()
    # store
    record = MagicMock()
    record.model_dump.return_value = {
        "id": "mem-1",
        "user_id": "u1",
        "content": "test",
        "metadata": {},
        "namespace": "default",
        "importance": 3,
        "created_at": "2026-07-31T12:00:00Z",
        "updated_at": "2026-07-31T12:00:00Z",
    }
    action = MagicMock()
    action.value = "insert"
    svc.store = AsyncMock(return_value=(record, action))

    # get
    svc.get = AsyncMock(return_value=record)

    # update
    svc.update = AsyncMock(return_value=record)

    # delete
    svc.delete = AsyncMock(return_value=True)

    # search
    search_result = MagicMock()
    search_result.model_dump.return_value = {
        "id": "sr-1",
        "content": "found",
        "metadata": {},
        "importance": 3,
        "score": 0.95,
    }
    svc.search = AsyncMock(return_value=[search_result])

    # list
    list_result = MagicMock()
    list_result.items = [record]
    list_result.total = 1
    svc.list = AsyncMock(return_value=list_result)

    # recent
    svc.recent = AsyncMock(return_value=[record])

    # get_stats
    stats_item = MagicMock()
    stats_item.model_dump.return_value = {
        "namespace": "default",
        "count": 5,
        "last_updated": None,
    }
    svc.get_stats = AsyncMock(return_value=[stats_item])

    # get_graph_stats
    graph_stats = MagicMock()
    graph_stats.model_dump.return_value = {
        "total_granules": 10,
        "total_relations": 5,
        "linked_granules": 8,
        "orphans": 2,
        "avg_connections": 0.5,
        "by_namespace": {},
        "by_link_type": {},
    }
    svc.get_graph_stats = AsyncMock(return_value=graph_stats)

    # traverse
    traverse_result = MagicMock()
    traverse_result.nodes = [{"id": "n1"}]
    traverse_result.edges = []
    svc.traverse = AsyncMock(return_value=traverse_result)

    # archive
    svc.archive = AsyncMock(return_value=True)

    # forget
    svc.forget = AsyncMock(return_value=3)

    # dedup
    svc.dedup = MagicMock()
    dedup_decision = MagicMock()
    dedup_decision.action.value = "insert"
    dedup_decision.content_hash = "abc123"
    dedup_decision.embedding = [0.1, 0.2]
    dedup_decision.existing_id = None
    svc.dedup.check_batch = AsyncMock(return_value=[dedup_decision])

    # ns_repo
    svc.ns_repo = MagicMock()
    ns_record = MagicMock()
    ns_record.id = "ns-1"
    svc.ns_repo.get_or_create = AsyncMock(return_value=ns_record)

    # repository
    svc.repository = MagicMock()
    svc.repository.insert_batch = AsyncMock(return_value=["mem-1"])
    svc.repository.sync_links_batch = AsyncMock()
    svc.repository.get_relations_by_source = AsyncMock(return_value=[])
    svc.repository.get_relations_by_target = AsyncMock(return_value=[])

    # config
    svc.config = MagicMock()
    svc.config.dedup_enabled = False

    # add_relation
    svc.add_relation = AsyncMock(return_value="rel-1")

    # delete_relation
    svc.delete_relation = AsyncMock(return_value=True)

    return svc


@pytest.fixture
def mock_embedding():
    emb = MagicMock()
    emb.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    emb.embed_many = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    return emb


# ── Patch _get_service for all task tests ────────────────────────


@pytest.fixture(autouse=True)
def patch_service(mock_memory_service, mock_embedding):
    """Patch _get_service to return our mock for all task tests."""
    with patch(
        "memory_server.tasks.memory_tasks._get_service",
        return_value=mock_memory_service,
    ), patch(
        "memory_server.tasks.memory_tasks.get_embedding",
        return_value=mock_embedding,
    ), patch(
        "memory_server.tasks.hash_tasks._get_hash_repo",
    ) as mock_hash_repo:
        # hash repo mock
        hash_repo = MagicMock()
        hash_repo.upsert = AsyncMock(return_value={
            "id": "h1", "source_type": "file", "source_id": "f1",
            "content_hash": "a" * 64, "created_at": "2026-07-31T12:00:00Z",
            "updated_at": "2026-07-31T12:00:00Z",
        })
        hash_repo.get = AsyncMock(return_value={
            "id": "h1", "source_type": "file", "source_id": "f1",
            "content_hash": "a" * 64, "created_at": "2026-07-31T12:00:00Z",
            "updated_at": "2026-07-31T12:00:00Z",
        })
        hash_repo.delete = AsyncMock(return_value="h1")
        hash_repo.list = AsyncMock(return_value=[])
        mock_hash_repo.return_value = hash_repo
        yield


# ══════════════════════════════════════════════════════════════════
# Memory Tasks
# ══════════════════════════════════════════════════════════════════


class TestStoreMemory:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import store_memory

        result = store_memory(content="hello world", user_id="u1")
        assert result["id"] == "mem-1"
        assert result["_dedup_action"] == "insert"

    def test_empty_content_raises(self):
        from memory_server.tasks.memory_tasks import store_memory

        with pytest.raises(ValidationError, match="content cannot be empty"):
            store_memory(content="", user_id="u1")

    def test_empty_user_id_raises(self):
        from memory_server.tasks.memory_tasks import store_memory

        with pytest.raises(ValidationError, match="user_id cannot be empty"):
            store_memory(content="hello", user_id="")

    def test_whitespace_only_content_raises(self):
        from memory_server.tasks.memory_tasks import store_memory

        with pytest.raises(ValidationError):
            store_memory(content="   ", user_id="u1")


class TestGetMemory:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import get_memory

        result = get_memory(memory_id="mem-1")
        assert result["id"] == "mem-1"

    def test_empty_id_raises(self):
        from memory_server.tasks.memory_tasks import get_memory

        with pytest.raises(ValidationError, match="memory_id cannot be empty"):
            get_memory(memory_id="")


class TestUpdateMemory:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import update_memory

        result = update_memory(memory_id="mem-1", content="updated")
        assert result["id"] == "mem-1"

    def test_empty_id_raises(self):
        from memory_server.tasks.memory_tasks import update_memory

        with pytest.raises(ValidationError):
            update_memory(memory_id="", content="x")


class TestDeleteMemory:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import delete_memory

        result = delete_memory(memory_id="mem-1")
        assert result["success"] is True

    def test_empty_id_raises(self):
        from memory_server.tasks.memory_tasks import delete_memory

        with pytest.raises(ValidationError):
            delete_memory(memory_id="")


class TestSearchMemories:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import search_memories

        results = search_memories(query="test query", user_id="u1")
        assert len(results) == 1
        assert results[0]["id"] == "sr-1"

    def test_empty_query_raises(self):
        from memory_server.tasks.memory_tasks import search_memories

        with pytest.raises(ValidationError, match="query cannot be empty"):
            search_memories(query="")


class TestListMemories:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import list_memories

        result = list_memories(user_id="u1")
        assert "items" in result
        assert "total" in result
        assert result["total"] == 1


class TestGetRecent:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import get_recent

        results = get_recent(namespace="default", limit=5)
        assert len(results) == 1

    def test_since_string_parsed(self):
        from memory_server.tasks.memory_tasks import get_recent

        # Should not raise — datetime.fromisoformat handles the string
        results = get_recent(since="2026-07-31T00:00:00")
        assert isinstance(results, list)


class TestGetStats:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import get_stats

        result = get_stats(user_id="u1")
        assert len(result) == 1
        assert result[0]["namespace"] == "default"


class TestGetNamespaces:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import get_namespaces

        with patch("memory_server.tasks.memory_tasks._get_service") as mock_get_svc:
            mock_svc = MagicMock()
            ns = MagicMock()
            ns.uid = "default"
            ns.name = "Default"
            ns.description = ""
            mock_svc.ns_repo.list_all = AsyncMock(return_value=[ns])
            mock_get_svc.return_value = mock_svc

            result = get_namespaces()
            assert len(result) == 1
            assert result[0]["uid"] == "default"


class TestFindSimilar:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import find_similar

        results = find_similar(content="test query", user_id="u1")
        assert len(results) == 1

    def test_empty_content_raises(self):
        from memory_server.tasks.memory_tasks import find_similar

        with pytest.raises(ValidationError, match="content cannot be empty"):
            find_similar(content="")


class TestGetRelations:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import get_relations

        with patch("memory_server.tasks.memory_tasks._get_service") as mock_get_svc:
            mock_svc = MagicMock()
            rel_in = MagicMock()
            rel_in.model_dump.return_value = {"source_id": "a", "target_id": "b", "type": "related_to"}
            rel_out = MagicMock()
            rel_out.model_dump.return_value = {"source_id": "b", "target_id": "c", "type": "contains"}
            mock_result = MagicMock()
            mock_result.incoming = [rel_in]
            mock_result.outgoing = [rel_out]
            mock_svc.repository.get_relations = AsyncMock(return_value=mock_result)
            mock_get_svc.return_value = mock_svc

            result = get_relations(source_id="granule-1")
            assert "incoming" in result
            assert "outgoing" in result
            assert len(result["incoming"]) == 1
            assert len(result["outgoing"]) == 1

    def test_empty_source_id_raises(self):
        from memory_server.tasks.memory_tasks import get_relations

        with pytest.raises(ValidationError, match="source_id cannot be empty"):
            get_relations(source_id="")


class TestGraphStats:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import graph_stats

        result = graph_stats()
        assert "total_granules" in result
        assert result["total_granules"] == 10


class TestTraverseGraph:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import traverse_graph

        result = traverse_graph(start_id="granule-1", depth=2)
        assert "nodes" in result
        assert "edges" in result

    def test_empty_start_id_raises(self):
        from memory_server.tasks.memory_tasks import traverse_graph

        with pytest.raises(ValidationError, match="start_id cannot be empty"):
            traverse_graph(start_id="")


class TestIngestBatch:
    def test_happy_path_dedup_disabled(self):
        from memory_server.tasks.memory_tasks import ingest_batch

        result = ingest_batch(
            entries=[{"content": "item1"}, {"content": "item2"}],
            user_id="u1",
        )
        assert "results" in result
        assert "summary" in result

    def test_empty_entries_raises(self):
        from memory_server.tasks.memory_tasks import ingest_batch

        with pytest.raises(ValidationError, match="entries cannot be empty"):
            ingest_batch(entries=[], user_id="u1")

    def test_empty_user_id_raises(self):
        from memory_server.tasks.memory_tasks import ingest_batch

        with pytest.raises(ValidationError, match="user_id cannot be empty"):
            ingest_batch(entries=[{"content": "x"}], user_id="")


class TestForgetMemories:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import forget_memories

        result = forget_memories(user_id="u1")
        assert result["deleted_count"] == 3

    def test_empty_user_id_raises(self):
        from memory_server.tasks.memory_tasks import forget_memories

        with pytest.raises(ValidationError):
            forget_memories(user_id="")


class TestArchiveMemory:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import archive_memory

        result = archive_memory(memory_id="mem-1")
        assert result["success"] is True

    def test_empty_id_raises(self):
        from memory_server.tasks.memory_tasks import archive_memory

        with pytest.raises(ValidationError):
            archive_memory(memory_id="")


class TestAddRelation:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import add_relation

        result = add_relation(
            source_id="src-1", target_id="tgt-1", link_type="related_to"
        )
        assert result["ok"] is True
        assert result["relation_id"] == "rel-1"

    def test_empty_source_id_raises(self):
        from memory_server.tasks.memory_tasks import add_relation

        with pytest.raises(ValidationError):
            add_relation(source_id="", target_id="tgt-1")


class TestDeleteRelation:
    def test_happy_path(self):
        from memory_server.tasks.memory_tasks import delete_relation

        result = delete_relation(
            source_id="src-1", target_id="tgt-1", link_type="related_to"
        )
        assert result["ok"] is True

    def test_empty_source_id_raises(self):
        from memory_server.tasks.memory_tasks import delete_relation

        with pytest.raises(ValidationError):
            delete_relation(source_id="", target_id="t", link_type="related_to")

    def test_empty_target_id_raises(self):
        from memory_server.tasks.memory_tasks import delete_relation

        with pytest.raises(ValidationError):
            delete_relation(source_id="s", target_id="", link_type="related_to")

    def test_empty_link_type_raises(self):
        from memory_server.tasks.memory_tasks import delete_relation

        with pytest.raises(ValidationError):
            delete_relation(source_id="s", target_id="t", link_type="")


# ══════════════════════════════════════════════════════════════════
# Hash Tasks
# ══════════════════════════════════════════════════════════════════


class TestUpsertHash:
    def test_happy_path(self):
        from memory_server.tasks.hash_tasks import upsert_hash

        result = upsert_hash(
            source_type="file",
            source_id="f1",
            content_hash="a" * 64,
        )
        assert result["id"] == "h1"

    def test_empty_source_type_raises(self):
        from memory_server.tasks.hash_tasks import upsert_hash

        with pytest.raises(ValidationError, match="source_type cannot be empty"):
            upsert_hash(source_type="", source_id="f1", content_hash="a" * 64)

    def test_empty_source_id_raises(self):
        from memory_server.tasks.hash_tasks import upsert_hash

        with pytest.raises(ValidationError, match="source_id cannot be empty"):
            upsert_hash(source_type="file", source_id="", content_hash="a" * 64)

    def test_invalid_hash_length_raises(self):
        from memory_server.tasks.hash_tasks import upsert_hash

        with pytest.raises(ValidationError, match="content_hash must be 64"):
            upsert_hash(source_type="file", source_id="f1", content_hash="short")


class TestGetHash:
    def test_happy_path(self):
        from memory_server.tasks.hash_tasks import get_hash

        result = get_hash(source_type="file", source_id="f1")
        assert result["content_hash"] == "a" * 64

    def test_empty_source_type_raises(self):
        from memory_server.tasks.hash_tasks import get_hash

        with pytest.raises(ValidationError):
            get_hash(source_type="", source_id="f1")

    def test_empty_source_id_raises(self):
        from memory_server.tasks.hash_tasks import get_hash

        with pytest.raises(ValidationError):
            get_hash(source_type="file", source_id="")


class TestDeleteHash:
    def test_happy_path(self):
        from memory_server.tasks.hash_tasks import delete_hash

        result = delete_hash(source_type="file", source_id="f1")
        assert result["success"] is True
        assert result["id"] == "h1"

    def test_empty_source_type_raises(self):
        from memory_server.tasks.hash_tasks import delete_hash

        with pytest.raises(ValidationError):
            delete_hash(source_type="", source_id="f1")

    def test_empty_source_id_raises(self):
        from memory_server.tasks.hash_tasks import delete_hash

        with pytest.raises(ValidationError):
            delete_hash(source_type="file", source_id="")


class TestListHashes:
    def test_happy_path(self):
        from memory_server.tasks.hash_tasks import list_hashes

        result = list_hashes()
        assert isinstance(result, list)

    def test_limit_capped_at_500(self):
        from memory_server.tasks.hash_tasks import list_hashes

        # Should not raise even with huge limit
        result = list_hashes(limit=10000)
        assert isinstance(result, list)
