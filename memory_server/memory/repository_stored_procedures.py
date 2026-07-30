"""
Обновлённые методы MemoryRepository, использующие хранимки.

Этот файл содержит ТОЛЬКО новые/изменённые методы.
Интеграция: заменить вызовы в repository.py на эти методы.
"""

from __future__ import annotations

import json
from datetime import datetime

import asyncpg

from memory_server.db import stored_procedure_queries as spq
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    MemoryStatsItem,
    Relation,
    RelationListResult,
    SearchResult,
    TraverseResult,
)


class MemoryRepositorySP:
    """Data access layer с хранимками (Stored Procedures)."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ── 1. UPSERT ──────────────────────────────────────────

    async def upsert(
        self,
        user_id: str,
        content: str,
        embedding: list[float],
        metadata: dict,
        namespace: str,
        namespace_id: str,
        content_hash: str | None = None,
        importance: int = 3,
    ) -> tuple[str, str]:
        """Upsert памяти. Возвращает (id, action)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                spq.CALL_MEMORY_UPSERT,
                user_id,
                content,
                embedding,
                metadata,
                namespace,
                namespace_id,
                content_hash,
                importance,
            )
            return str(row["id"]), row["action"]

    # ── 2. BATCH INSERT WITH DEDUP ────────────────────────

    async def insert_batch(
        self,
        user_ids: list[str],
        contents: list[str],
        embeddings: list[str],
        metadatas: list[dict],
        namespaces: list[str],
        namespace_ids: list[str],
        content_hashes: list[str | None],
        importances: list[int] | None = None,
    ) -> list[str]:
        """Batch insert с exact dedup на уровне БД.

        embeddings: text[] — pgvector cast to vector в SQL.
        Возвращает id только ВСТАВЛЕННЫХ записей (дубли пропущены).
        """
        if importances is None:
            importances = [3] * len(user_ids)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                spq.CALL_MEMORY_INSERT_BATCH,
                user_ids,
                contents,
                embeddings,
                metadatas,
                namespaces,
                namespace_ids,
                content_hashes,
                importances,
            )
            return [str(row["id"]) for row in rows]

    # ── 3. SEMANTIC SEARCH ─────────────────────────────────

    async def search(
        self,
        query_embedding: list[float],
        user_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
        namespace: str | None = None,
    ) -> list[SearchResult]:
        """Semantic search через хранимку. Автоматически использует HNSW если есть."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                spq.CALL_MEMORY_SEARCH_HNSW,
                query_embedding,
                user_id,
                namespace,
                threshold,
                limit,
            )
            return [
                SearchResult(
                    id=str(row["id"]),
                    content=row["content"],
                    metadata=row["metadata"] or {},
                    importance=row["importance"],
                    score=float(row["score"]),
                )
                for row in rows
            ]

    # ── 4. GRAPH STATS UNIFIED ─────────────────────────────

    async def get_graph_stats(self) -> GraphStats:
        """Статистика графа — один запрос вместо трёх."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(spq.CALL_GRAPH_STATS_UNIFIED)

            total_granules = row["p_total_granules"]
            total_relations = row["p_total_relations"]
            linked_granules = row["p_linked_granules"]
            orphans = row["p_orphans"]
            avg = (total_relations * 2 / total_granules) if total_granules > 0 else 0.0

            # by_namespace и by_link_type уже в JSONB
            by_namespace = json.loads(row["p_by_namespace"]) if row["p_by_namespace"] else {}
            by_link_type = json.loads(row["p_by_link_type"]) if row["p_by_link_type"] else {}

            return GraphStats(
                total_granules=total_granules,
                total_relations=total_relations,
                linked_granules=linked_granules,
                orphans=orphans,
                avg_connections=round(avg, 2),
                by_namespace=by_namespace,
                by_link_type=by_link_type,
            )

    # ── 5. GRAPH TRAVERSE FULL ─────────────────────────────

    async def traverse(
        self, start_id: str, depth: int = 3, link_types: list[str] | None = None
    ) -> TraverseResult:
        """Обход графа с полным возвратом нод и рёбер — один запрос вместо 2N+2."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                spq.CALL_GRAPH_TRAVERSE_FULL,
                start_id,
                depth,
                link_types,
            )

            nodes_raw = json.loads(row["nodes"]) if row["nodes"] else []
            edges_raw = json.loads(row["edges"]) if row["edges"] else []

            # Конвертируем edges в Relation-объекты
            edges = [
                Relation(
                    id=str(e["id"]),
                    source_id=str(e["source_id"]),
                    target_id=str(e["target_id"]) if e.get("target_id") else None,
                    target_name=e.get("target_name"),
                    link_type=e["link_type"],
                    description=e.get("description"),
                    weight=float(e.get("weight", 1.0)),
                    metadata=e.get("metadata") or {},
                    created_at=datetime.now(),  # edges JSON не содержит created_at
                )
                for e in edges_raw
            ]

            return TraverseResult(nodes=nodes_raw, edges=edges)

    # ── Остальные методы без изменений ─────────────────────
    # (get_by_id, find_by_content_hash, update, delete,
    #  list, recent, forget, get_stats, archive,
    #  add_relation, get_relations_by_source/target,
    #  delete_relation, delete_relations_by_source,
    #  find_relations_between)
    # — остаются как в текущем repository.py
