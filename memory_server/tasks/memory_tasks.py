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

from memory_server.logger import get_logger
from memory_server.state import get_state
from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask
from memory_server.tasks.errors import ValidationError

logger = get_logger(__name__)

_service: Any | None = None


def _get_service():
    """Get MemoryService via process-wide SeltiState (composition root).

    Singleton на модуль: сборка один раз, дальше мгновенный возврат инстанса.
    MemoryService и репозитории stateless — безопасно шарить между задачами;
    TTL-кеш NamespaceRepository корректно живёт с переиспользованием.
    """
    global _service
    if _service is None:
        _service = run_async(get_state().get_memory_service)
    return _service


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
    project_id: str | None = None,
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
        project_id=project_id,
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
    project_id: str | None = None,
    supersedes: str | None = None,
    clear_project_id: bool = False,
) -> dict[str, Any]:
    """Update an existing memory record.

    supersedes: ID замещаемой версии — старая закрывается (status='superseded').
    """
    if not memory_id or not memory_id.strip():
        raise ValidationError("memory_id cannot be empty")

    service = _get_service()
    record = run_async(
        service.update,
        memory_id=memory_id,
        content=content,
        metadata=metadata,
        importance=importance,
        project_id=project_id,
        supersedes=supersedes,
        clear_project_id=clear_project_id,
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
    project_id: str | None = None,
    include_historical: bool = False,
    created_after: str | None = None,
    created_before: str | None = None,
    status: str | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Search memories (hybrid: dense + FTS, Фаза 1.1).

    created_after/created_before — ISO-строки (Celery JSON не несёт
    datetime), парсятся здесь; REST-фильтры /api/search (Фаза 5.1).
    offset — пагинация /api/search (Фаза 5.2).
    """
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
        project_id=project_id,
        include_historical=include_historical,
        created_after=datetime.fromisoformat(created_after) if created_after else None,
        created_before=datetime.fromisoformat(created_before) if created_before else None,
        status=status,
        offset=offset,
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
    project_id: str | None = None,
) -> dict[str, Any]:
    """List memory records with pagination."""
    service = _get_service()
    result = run_async(
        service.list,
        user_id=user_id,
        namespace=namespace,
        limit=limit,
        offset=offset,
        project_id=project_id,
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
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Get recent memory records."""
    since_dt = datetime.fromisoformat(since) if since else None
    service = _get_service()
    results = run_async(
        service.recent,
        namespace=namespace,
        since=since_dt,
        limit=limit,
        project_id=project_id,
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
def get_stats(
    self, user_id: str | None = None, project_id: str | None = None
) -> list[dict[str, Any]]:
    """Get memory statistics per namespace; project_id — срез по проекту."""
    service = _get_service()
    result = run_async(service.get_stats, user_id=user_id, project_id=project_id)
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
    service = _get_service()
    namespaces = run_async(service.ns_repo.list_all)
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
    project_id: str | None = None,
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
        project_id=project_id,
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
    """Get incoming and outgoing relations for a granule. One query via UNION ALL."""
    if not source_id or not source_id.strip():
        raise ValidationError("source_id cannot be empty")

    service = _get_service()
    result = run_async(
        service.repository.get_relations,
        source_id,
        link_type,
    )
    return {
        "incoming": [r.model_dump(mode="json") for r in result.incoming],
        "outgoing": [r.model_dump(mode="json") for r in result.outgoing],
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
    limit: int | None = None,
    offset: int = 0,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Traverse the knowledge graph from a starting node.

    limit/offset — курсорная пагинация узлов (cap traverse_max_nodes).
    project_id — ранняя валидация проекта (slug/UUID); граф связей
    глобальный, фильтра узлов нет.
    """
    if not start_id or not start_id.strip():
        raise ValidationError("start_id cannot be empty")

    service = _get_service()
    result = run_async(
        service.traverse,
        start_id=start_id,
        depth=depth,
        link_types=link_types,
        limit=limit,
        offset=offset,
        project_id=project_id,
    )
    return {
        "nodes": result.nodes,
        "edges": [e.model_dump(mode="json") for e in result.edges],
        "total_nodes": result.total_nodes,
        "truncated": result.truncated,
    }


# ── Ingest Batch ────────────────────────────────────────────────


def _split_valid_entries(entries: list[Any]) -> tuple[list[dict], list[dict]]:
    """Валидация батча до dedup: content — обязательная непустая строка.

    Голая строка нормализуется в запись (клиент мог потерять dict-обёртку);
    malformed-записи не роняют батч — возвращаются отдельно с исходным индексом
    для warning-лога и счётчика в результате.
    """
    valid: list[dict] = []
    malformed: list[dict] = []
    for idx, entry in enumerate(entries):
        if isinstance(entry, str):
            entry = {"content": entry}
        content = entry.get("content") if isinstance(entry, dict) else None
        if isinstance(content, str) and content.strip():
            valid.append(entry)
        else:
            malformed.append({"index": idx, "error": "content must be a non-empty string"})
    return valid, malformed


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
    project_id: str | None = None,
) -> dict[str, Any]:
    """Store multiple memory records in batch (project_id — один на весь батч)."""
    if not entries:
        raise ValidationError("entries cannot be empty")
    if not user_id or not user_id.strip():
        raise ValidationError("user_id cannot be empty")

    service = _get_service()

    # Валидация до dedup: malformed-записи пропускаем, батч не роняем
    # (прод-инцидент: KeyError('content') в DedupEngine.check_batch)
    valid_entries, malformed = _split_valid_entries(entries)
    summary: dict[str, int] = {"insert": 0, "skip": 0, "update": 0, "invalid": 0}
    results = []
    for bad in malformed:
        summary["invalid"] += 1
        results.append({"id": None, "action": "invalid", "error": bad["error"]})
    if malformed:
        logger.warning("ingest_batch: skipped malformed entries", extra={
            "indices": [bad["index"] for bad in malformed],
            "invalid": summary["invalid"],
        })

    to_insert: list[dict] = []
    # (index в results, content_hash): intra-batch дубли — existing_id появится
    # только после INSERT первого вхождения, проставляем пост-фактум
    pending_dup_links: list[tuple[int, str]] = []

    if service.config.dedup_enabled:
        decisions = run_async(service.dedup.check_batch, valid_entries, user_id)
        for entry, decision in zip(valid_entries, decisions):
            ns = entry.get("namespace", "default")
            entry_metadata = entry.get("metadata")
            if decision.action.value in ("skip", "update"):
                summary[decision.action.value] += 1
                results.append({
                    "id": decision.existing_id,
                    "action": decision.action.value,
                    "namespace": ns,
                })
                if decision.existing_id is None:
                    pending_dup_links.append(
                        (len(results) - 1, decision.content_hash)
                    )
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
        for entry in valid_entries:
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
            embeddings = run_async(service.embedding.embed_many, texts_to_embed)
            for idx, emb in zip(indices_to_embed, embeddings):
                to_insert[idx]["embedding"] = emb

        # Resolve namespace ids + project id
        ns_names = [item["namespace"] for item in to_insert]
        ns_records = [
            run_async(service.ns_repo.get_or_create, ns) for ns in ns_names
        ]
        namespace_ids = [ns_record.id for ns_record in ns_records]
        resolved_project = run_async(service.resolve_project, project_id)
        project_ids = [resolved_project] * len(to_insert)

        # Batch insert
        emb_list = [item["embedding"] for item in to_insert]
        ids = run_async(
            service.repository.insert_batch,
            user_ids=[user_id] * len(to_insert),
            contents=[item["content"] for item in to_insert],
            embeddings=emb_list,
            metadatas=[item["metadata"] for item in to_insert],
            namespace_ids=namespace_ids,
            content_hashes=[item["content_hash"] for item in to_insert],
            project_ids=project_ids,
            importances=[item["importance"] for item in to_insert],
        )
        for rid, item in zip(ids, to_insert):
            summary["insert"] += 1
            results.append({
                "id": rid,
                "action": "insert",
                "namespace": item["namespace"],
            })

        # Intra-batch дубли: ссылка на первую вставленную запись того же хеша
        if pending_dup_links:
            inserted_by_hash = {
                item["content_hash"]: rid for rid, item in zip(ids, to_insert)
            }
            for result_idx, dup_hash in pending_dup_links:
                results[result_idx]["id"] = inserted_by_hash.get(dup_hash)

    # Sync links
    all_ids = [r["id"] for r in results if r["id"]]
    if all_ids:
        try:
            run_async(service.repository.sync_links_batch, all_ids)
        except Exception:
            logger.exception("sync_links_batch failed (non-fatal)")

    logger.info("ingest_batch: DONE", extra={"summary": summary})
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
    project_id: str | None = None,
) -> dict[str, Any]:
    """Delete all memories for a user, optionally filtered by namespace/project."""
    if not user_id or not user_id.strip():
        raise ValidationError("user_id cannot be empty")

    service = _get_service()
    deleted = run_async(
        service.forget, user_id=user_id, namespace=namespace, project_id=project_id
    )
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
    """Retract a memory record (soft delete) — единый путь service.retract."""
    if not memory_id or not memory_id.strip():
        raise ValidationError("memory_id cannot be empty")

    service = _get_service()
    success = run_async(service.retract, memory_id=memory_id)
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
    project_id: str | None = None,
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
        project_id=project_id,
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


# ── Supersession / lifecycle tools (Фаза 2.1) ──────────────────


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.supersede_memory",
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
def supersede_memory(
    self,
    granule_id: str,
    content: str,
    metadata: dict | None = None,
    importance: int | None = None,
) -> dict[str, Any]:
    """Create a new version of a granule (fact conflict resolution).

    Старая закрывается (status='superseded', valid_to=valid_from новой);
    новая наследует user/namespace/project/version+1/metadata, confidence ×0.9.
    """
    if not granule_id or not granule_id.strip():
        raise ValidationError("granule_id cannot be empty")
    if not content or not content.strip():
        raise ValidationError("content cannot be empty")

    service = _get_service()
    record = run_async(
        service.create_version,
        granule_id=granule_id,
        new_content=content,
        metadata_merge=metadata,
        importance=importance,
    )
    return record.model_dump(mode="json")


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.get_memory_history",
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
def get_memory_history(self, granule_id: str) -> dict[str, Any]:
    """Supersession-цепочка гранулы: от старейшей к новейшей, current_id помечен."""
    if not granule_id or not granule_id.strip():
        raise ValidationError("granule_id cannot be empty")

    service = _get_service()
    history = run_async(service.get_history, granule_id=granule_id)
    return {
        "items": [r.model_dump(mode="json") for r in history.items],
        "current_id": history.current_id,
    }


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.freeze_memory",
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
def freeze_memory(self, granule_id: str, frozen: bool) -> dict[str, Any]:
    """Ручная заморозка вечных фактов (D4): защита от decay/GC."""
    if not granule_id or not granule_id.strip():
        raise ValidationError("granule_id cannot be empty")

    service = _get_service()
    record = run_async(service.freeze, memory_id=granule_id, frozen=frozen)
    return record.model_dump(mode="json")


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.stale_list",
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
def stale_list(
    self,
    user_id: str | None = None,
    namespace: str | None = None,
    project_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Кандидаты на ревизию: asserted, confidence < порога, нет доступа N дней."""
    service = _get_service()
    records = run_async(
        service.stale_list,
        user_id=user_id,
        namespace=namespace,
        project_id=project_id,
        limit=limit,
    )
    return [r.model_dump(mode="json") for r in records]


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.memory_tasks.cluster_list",
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
def cluster_list(
    self,
    namespace: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Обзор кластеров Level 2 (graceful до миграции 022)."""
    service = _get_service()
    return run_async(
        service.cluster_list,
        namespace=namespace,
        project_id=project_id,
    )
