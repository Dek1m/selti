"""Memory tasks for Celery workers.

Each task wraps an async operation from MemoryService using run_async.
Tasks are routed to the 'memory' queue (except ingest_batch → 'batch').

Timeouts per plan v3:
- memory_ops: soft=240s, hard=300s
- batch_ops: soft=600s, hard=900s
"""

import logging
from datetime import datetime
from typing import Any

from celery import shared_task

from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask
from memory_server.tasks.connections import get_pool, get_qdrant, get_embedding
from memory_server.tasks.errors import ValidationError

logger = logging.getLogger(__name__)


def _get_service():
    """Get MemoryService with worker-scoped connections (Qdrant primary + SQL fallback)."""
    from memory_server.memory.repository_qdrant import MemoryRepository
    from memory_server.memory.namespace_repository import NamespaceRepository
    from memory_server.memory.dedup import DedupEngine
    from memory_server.memory.service import MemoryService
    from memory_server.config import settings

    pool = get_pool()
    qdrant = get_qdrant()
    repository = MemoryRepository(pool, qdrant=qdrant, qdrant_collection=settings.qdrant_collection)
    ns_repo = NamespaceRepository(pool)
    embedding = get_embedding()
    dedup = DedupEngine(repository, embedding, settings)
    return MemoryService(
        repository=repository,
        embedding_provider=embedding,
        namespace_repository=ns_repo,
        config=settings,
    )


# ── Store ───────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.store_memory",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def store_memory(
    self,
    content: str,
    user_id: str,
    metadata: dict | None = None,
    namespace: str | None = None,
    importance: int | None = None,
) -> dict[str, Any]:
    """Store a new memory record with deduplication."""
    if not content or not content.strip():
        raise ValidationError("content cannot be empty")
    if not user_id or not user_id.strip():
        raise ValidationError("user_id cannot be empty")

    service = _get_service()
    record, action = run_async(
        service.store,
        content=content,
        user_id=user_id,
        metadata=metadata,
        namespace=namespace,
        importance=importance,
    )
    result = record.model_dump(mode="json")
    result["_dedup_action"] = action.value
    return result


# ── Get ─────────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.get_memory",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def get_memory(self, memory_id: str) -> dict[str, Any]:
    """Retrieve a single memory record by ID."""
    if not memory_id or not memory_id.strip():
        raise ValidationError("memory_id cannot be empty")

    service = _get_service()
    record = run_async(service.get, memory_id=memory_id)
    return record.model_dump(mode="json")


# ── Update ──────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.update_memory",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def update_memory(
    self,
    memory_id: str,
    content: str | None = None,
    metadata: dict | None = None,
    importance: int | None = None,
) -> dict[str, Any]:
    """Update an existing memory record."""
    if not memory_id or not memory_id.strip():
        raise ValidationError("memory_id cannot be empty")

    service = _get_service()
    record = run_async(
        service.update,
        memory_id=memory_id,
        content=content,
        metadata=metadata,
        importance=importance,
    )
    return record.model_dump(mode="json")


# ── Delete ──────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.delete_memory",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def delete_memory(self, memory_id: str) -> dict[str, Any]:
    """Delete a memory record by ID."""
    if not memory_id or not memory_id.strip():
        raise ValidationError("memory_id cannot be empty")

    service = _get_service()
    success = run_async(service.delete, memory_id=memory_id)
    return {"success": success}


# ── Search ──────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.search_memories",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def search_memories(
    self,
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Search memories by semantic similarity."""
    if not query or not query.strip():
        raise ValidationError("query cannot be empty")

    service = _get_service()
    results = run_async(
        service.search,
        query=query,
        user_id=user_id,
        limit=limit,
        threshold=threshold,
        namespace=namespace,
    )
    return [r.model_dump(mode="json") for r in results]


# ── List ────────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.list_memories",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def list_memories(
    self,
    user_id: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List memory records with pagination."""
    service = _get_service()
    result = run_async(
        service.list,
        user_id=user_id,
        namespace=namespace,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [r.model_dump(mode="json") for r in result.items],
        "total": result.total,
    }


# ── Recent ──────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.get_recent",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def get_recent(
    self,
    namespace: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get recent memory records."""
    since_dt = datetime.fromisoformat(since) if since else None
    service = _get_service()
    results = run_async(
        service.recent,
        namespace=namespace,
        since=since_dt,
        limit=limit,
    )
    return [r.model_dump(mode="json") for r in results]


# ── Stats ───────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.get_stats",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def get_stats(self, user_id: str | None = None) -> list[dict[str, Any]]:
    """Get memory statistics per namespace."""
    service = _get_service()
    result = run_async(service.get_stats, user_id=user_id)
    return [item.model_dump(mode="json") for item in result]


# ── Namespaces ──────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.get_namespaces",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def get_namespaces(self) -> list[dict[str, Any]]:
    """Get list of all namespaces."""
    pool = get_pool()
    from memory_server.memory.namespace_repository import NamespaceRepository

    ns_repo = NamespaceRepository(pool)
    namespaces = run_async(ns_repo.list_all)
    return [
        {"uid": ns.uid, "name": ns.name, "description": ns.description}
        for ns in namespaces
    ]


# ── Find Similar ────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.find_similar",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def find_similar(
    self,
    content: str,
    user_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Find semantically similar memories without storing."""
    if not content or not content.strip():
        raise ValidationError("content cannot be empty")

    service = _get_service()
    results = run_async(
        service.search,
        query=content,
        user_id=user_id,
        limit=limit,
        threshold=threshold,
        namespace=namespace,
    )
    return [r.model_dump(mode="json") for r in results]


# ── Get Relations ───────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.get_relations",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def get_relations(
    self,
    source_id: str,
    link_type: str | None = None,
) -> dict[str, Any]:
    """Get incoming and outgoing relations for a granule."""
    if not source_id or not source_id.strip():
        raise ValidationError("source_id cannot be empty")

    service = _get_service()
    outgoing = run_async(
        service.repository.get_relations_by_source,
        source_id,
        link_type,
    )
    incoming = run_async(
        service.repository.get_relations_by_target,
        source_id,
        link_type,
    )
    return {
        "incoming": [r.model_dump(mode="json") for r in incoming],
        "outgoing": [r.model_dump(mode="json") for r in outgoing],
    }


# ── Graph Stats ─────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.graph_stats",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def graph_stats(self) -> dict[str, Any]:
    """Get knowledge graph statistics."""
    service = _get_service()
    stats = run_async(service.get_graph_stats)
    return stats.model_dump(mode="json")


# ── Traverse ────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.traverse_graph",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def traverse_graph(
    self,
    start_id: str,
    depth: int = 3,
    link_types: list[str] | None = None,
) -> dict[str, Any]:
    """Traverse the knowledge graph from a starting node."""
    if not start_id or not start_id.strip():
        raise ValidationError("start_id cannot be empty")

    service = _get_service()
    result = run_async(
        service.traverse,
        start_id=start_id,
        depth=depth,
        link_types=link_types,
    )
    return {
        "nodes": result.nodes,
        "edges": [e.model_dump(mode="json") for e in result.edges],
    }


# ── Ingest Batch ────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.ingest_batch",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=600,
    time_limit=900,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="batch",
    routing_key="batch",
)
def ingest_batch(
    self,
    entries: list[dict],
    user_id: str,
) -> dict[str, Any]:
    """Store multiple memory records in batch."""
    if not entries:
        raise ValidationError("entries cannot be empty")
    if not user_id or not user_id.strip():
        raise ValidationError("user_id cannot be empty")

    service = _get_service()

    # Batch dedup
    summary: dict[str, int] = {"insert": 0, "skip": 0, "update": 0}
    results = []
    to_insert: list[dict] = []

    if service.config.dedup_enabled:
        decisions = run_async(service.dedup.check_batch, entries, user_id)
        for entry, decision in zip(entries, decisions):
            ns = entry.get("namespace", "default")
            entry_metadata = entry.get("metadata")
            if decision.action.value in ("skip", "update"):
                summary[decision.action.value] += 1
                results.append({
                    "id": decision.existing_id,
                    "action": decision.action.value,
                    "namespace": ns,
                })
                continue
            to_insert.append({
                "content": entry["content"],
                "metadata": entry_metadata or {},
                "namespace": ns,
                "importance": entry.get("importance", 3),
                "content_hash": decision.content_hash,
                "embedding": decision.embedding,
            })
    else:
        for entry in entries:
            ns = entry.get("namespace", "default")
            entry_metadata = entry.get("metadata")
            to_insert.append({
                "content": entry["content"],
                "metadata": entry_metadata or {},
                "namespace": ns,
                "importance": entry.get("importance", 3),
                "content_hash": None,
                "embedding": None,
            })

    # Batch embed
    if to_insert:
        texts_to_embed = [
            item["content"] for item in to_insert if item["embedding"] is None
        ]
        indices_to_embed = [
            i for i, item in enumerate(to_insert) if item["embedding"] is None
        ]

        if texts_to_embed:
            embedding = get_embedding()
            embeddings = run_async(embedding.embed_many, texts_to_embed)
            for idx, emb in zip(indices_to_embed, embeddings):
                to_insert[idx]["embedding"] = emb

        # Resolve namespace_ids
        ns_names = [item["namespace"] for item in to_insert]
        ns_records = [
            run_async(service.ns_repo.get_or_create, ns) for ns in ns_names
        ]
        namespace_ids = [ns_record.id for ns_record in ns_records]

        # Batch insert
        if to_insert:
            ids = run_async(
                service.repository.insert_batch,
                user_ids=[user_id] * len(to_insert),
                contents=[item["content"] for item in to_insert],
                embeddings=[str(item["embedding"]) for item in to_insert],
                metadatas=[item["metadata"] for item in to_insert],
                namespaces=[item["namespace"] for item in to_insert],
                namespace_ids=namespace_ids,
                content_hashes=[item["content_hash"] for item in to_insert],
                importances=[item["importance"] for item in to_insert],
            )
            for rid, item in zip(ids, to_insert):
                summary["insert"] += 1
                results.append({
                    "id": rid,
                    "action": "insert",
                    "namespace": item["namespace"],
                })

    # Sync links
    all_ids = [r["id"] for r in results if r["id"]]
    if all_ids:
        try:
            run_async(service.repository.sync_links_batch, all_ids)
        except Exception:
            logger.exception("sync_links_batch failed (non-fatal)")

    return {"results": results, "summary": summary}


# ── Forget ──────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.forget_memories",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def forget_memories(
    self,
    user_id: str,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Delete all memories for a user, optionally filtered by namespace."""
    if not user_id or not user_id.strip():
        raise ValidationError("user_id cannot be empty")

    service = _get_service()
    deleted = run_async(service.forget, user_id=user_id, namespace=namespace)
    return {"deleted_count": deleted}


# ── Archive ────────────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.archive_memory",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def archive_memory(self, memory_id: str) -> dict[str, Any]:
    """Archive a memory record (soft delete)."""
    if not memory_id or not memory_id.strip():
        raise ValidationError("memory_id cannot be empty")

    service = _get_service()
    success = run_async(service.archive, memory_id=memory_id)
    return {"success": success}


# ── Add Relation ───────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.add_relation",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def add_relation(
    self,
    source_id: str,
    target_id: str | None = None,
    target_name: str | None = None,
    link_type: str = "related_to",
    description: str | None = None,
    weight: float = 1.0,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Create a relation between two granules."""
    if not source_id or not source_id.strip():
        raise ValidationError("source_id cannot be empty")

    service = _get_service()
    rel_id = run_async(
        service.add_relation,
        source_id=source_id,
        target_id=target_id,
        target_name=target_name,
        link_type=link_type,
        description=description,
        weight=weight,
        metadata=metadata,
    )
    return {"ok": True, "relation_id": rel_id}


# ── Delete Relation ────────────────────────────────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.delete_relation",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def delete_relation(
    self,
    source_id: str,
    target_id: str,
    link_type: str,
) -> dict[str, Any]:
    """Delete a relation between two granules."""
    if not source_id or not source_id.strip():
        raise ValidationError("source_id cannot be empty")
    if not target_id or not target_id.strip():
        raise ValidationError("target_id cannot be empty")
    if not link_type or not link_type.strip():
        raise ValidationError("link_type cannot be empty")

    service = _get_service()
    deleted = run_async(
        service.delete_relation,
        source_id=source_id,
        target_id=target_id,
        link_type=link_type,
    )
    return {"ok": deleted}
