"""PostgreSQL-only repository for memory records.

Хранит: метаданные, контент, связи, граф, контексты проектов.
НЕ хранит вектора — для этого QdrantStore.

Контракт волны 2 (Фаза 0.4): namespace приходит как namespace_id UUID
(резолв имени → id делает фасад через NamespaceRepository), актуальность
гранулы = status='asserted' AND valid_to IS NULL.
"""
from __future__ import annotations

import uuid as uuid_module
from datetime import datetime

import asyncpg

from memory_server.db import queries as q
from memory_server.exceptions import ConflictError, DatabaseError, SchemaPendingError
from memory_server.logger import get_logger
from memory_server.metrics import RELATIONS_REWIRED_TOTAL
from memory_server.models import (
    GraphStats,
    MemoryListResult,
    MemoryRecord,
    MemoryStatsItem,
    Relation,
    RelationListResult,
)

logger = get_logger(__name__)


class PostgreSQLRepository:
    """Data access layer for PostgreSQL — метаданные, связи, граф, контексты."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @staticmethod
    def _to_record(row: asyncpg.Record) -> MemoryRecord:
        """Row канонической проекции (_MEMORY_COLUMNS) → MemoryRecord."""
        return MemoryRecord(
            id=str(row["id"]),
            user_id=row["user_id"],
            content=row["content"],
            metadata=row["metadata"] or {},
            namespace=row["namespace"],
            importance=row["importance"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content_hash=row["content_hash"],
            project_id=row["project_id"],
            status=row["status"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            ingested_at=row["ingested_at"],
            confidence=row["confidence"],
            supersedes=row["supersedes"],
            superseded_by=row["superseded_by"],
            frozen=row["frozen"],
            # Guard вместо row[...]: старые моки/проекции без access-полей
            last_accessed_at=row["last_accessed_at"] if "last_accessed_at" in row else None,
            access_count=row["access_count"] if "access_count" in row else 0,
        )

    # ════════════════════════════════════════════════════════════
    # INSERT
    # ════════════════════════════════════════════════════════════

    async def insert(
        self,
        user_id: str,
        content: str,
        metadata: dict | None = None,
        namespace_id: str | None = None,
        content_hash: str | None = None,
        importance: int = 3,
        project_id: str | None = None,
        confidence: float | None = None,
        frozen: bool = False,
        supersedes: str | None = None,
    ) -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.INSERT_MEMORY,
                user_id,
                content,
                metadata or {},
                namespace_id,
                content_hash,
                importance,
                project_id,
                confidence,
                frozen,
                supersedes,
            )
            return str(row["id"])

    async def insert_batch(
        self,
        user_ids: list[str],
        contents: list[str],
        namespace_ids: list[str],
        content_hashes: list[str | None],
        project_ids: list[str | None],
        metadatas: list[dict] | None = None,
        importances: list[int] | None = None,
    ) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.INSERT_MEMORY_BATCH,
                user_ids,
                contents,
                metadatas or [{}] * len(user_ids),
                namespace_ids,
                content_hashes,
                importances or [3] * len(user_ids),
                project_ids,
            )
            return [str(row["id"]) for row in rows]

    # ════════════════════════════════════════════════════════════
    # SEARCH (SQL FTS fallback)
    # ════════════════════════════════════════════════════════════

    async def search_fts(
        self,
        query_text: str,
        user_id: str | None = None,
        namespace_id: str | None = None,
        project_id: str | None = None,
        limit: int = 10,
        include_historical: bool = False,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        status: str | None = None,
        entity_type: str | None = None,
    ) -> list[dict]:
        """Full-text search (russian): канал B гибрида + fallback без Qdrant.

        Возвращает полные строки проекции + score — гибридной сборке нужны
        ранжирующие поля (created_at/last_accessed_at/frozen/importance).
        created_after/created_before/status/entity_type — REST-фильтры
        /api/search (5.1/5.2).
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.SEARCH_MEMORIES,
                query_text,
                user_id,
                namespace_id,
                project_id,
                limit,
                include_historical,
                created_after,
                created_before,
                status,
                entity_type,
            )
            return [{**dict(row), "score": float(row["score"])} for row in rows]

    # ════════════════════════════════════════════════════════════
    # READ
    # ════════════════════════════════════════════════════════════

    async def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.SELECT_MEMORY_BY_ID, memory_id)
            return self._to_record(row) if row is not None else None

    async def find_by_entity_name(self, entity_name: str) -> MemoryRecord | None:
        """Найти гранулу по entity_name в metadata (fallback для строковых ID)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.SELECT_MEMORY_BY_ENTITY_NAME, entity_name)
            return self._to_record(row) if row is not None else None

    async def find_by_content_hash(
        self, namespace: str, content_hash: str
    ) -> MemoryRecord | None:
        """Exact-dedup lookup: uid → namespace_id резолвит БД (JOIN по UNIQUE-индексу)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.SELECT_MEMORY_BY_CONTENT_HASH, namespace, content_hash
            )
            return self._to_record(row) if row is not None else None

    async def find_by_content_hashes(
        self, ns_uids: list[str], content_hashes: list[str]
    ) -> dict[tuple[str, str], MemoryRecord]:
        """Batch exact-dedup (Фаза 1.4): один запрос на все пары (uid, hash).

        Возвращает {(namespace, content_hash): record} — совпадения только
        актуальных гранул (status/valid_to фильтр в SQL).
        """
        if not ns_uids:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.SELECT_MEMORY_BY_CONTENT_HASHES, ns_uids, content_hashes
            )
            return {
                (row["ns_uid"], row["matched_hash"]): self._to_record(row)
                for row in rows
            }

    async def bump_access(self, memory_ids: list[str]) -> int:
        """Инкремент access_count/last_accessed_at по выданным id (Фаза 1.2).

        Отдельное соединение из пула — вне транзакции чтения поиска.
        """
        if not memory_ids:
            return 0
        async with self.pool.acquire() as conn:
            result = await conn.execute(q.BUMP_ACCESS_MEMORIES, memory_ids)
            return int(result.split()[-1])

    async def map_layout_manual(
        self, node_id: str, x: float, y: float, z: float
    ) -> bool:
        """Ручные координаты гранулы на 3D-карте (миграция 025, UPSERT).

        Новая строка — rev=0 (вне поколений galactic: version-hash снапшота
        не дёргается), существующая — только x/y/z + source/updated_at.
        False = миграция 025 ещё не применена — координаты фича карты,
        не ядра памяти: тихая деградация, store не ронять.
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(q.MAP_LAYOUT_MANUAL_UPSERT_SQL, node_id, x, y, z)
            return True
        except asyncpg.UndefinedColumnError:
            logger.warning(
                "map_layout_manual: column 'source' missing (migration 025 pending)",
                extra={"node_id": node_id},
            )
            return False

    async def fetch_by_ids(
        self,
        ids: list[str],
        include_historical: bool = False,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        status: str | None = None,
        entity_type: str | None = None,
    ) -> list[dict]:
        """Batch fetch метаданных по IDs (для Qdrant-выдачи).

        Фильтр актуальности (status/valid_to) применён в SQL ДО обрезки
        limit — ретрактнутые гранулы не съедают лимит выдачи (Фаза 1.3);
        include_historical=True — time-travel, фильтр отключается.
        Семантика $2 зеркалит SEARCH_MEMORIES.$6: True → без фильтра.
        created_after/created_before/status/entity_type — REST-фильтры
        /api/search (5.1/5.2), применяются к кандидатам до RRF-fusion.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.FETCH_MEMORIES_BY_IDS,
                ids,
                include_historical,
                created_after,
                created_before,
                status,
                entity_type,
            )
            return [dict(row) for row in rows]

    async def list(
        self,
        user_id: str | None = None,
        namespace_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.LIST_MEMORIES, user_id, namespace_id, project_id, limit, offset
            )
            items = [self._to_record(row) for row in rows]
            total = rows[0]["total_count"] if rows else 0
            return MemoryListResult(items=items, total=total)

    async def recent(
        self,
        namespace_id: str | None = None,
        project_id: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.RECENT_MEMORIES, namespace_id, project_id, since, limit
            )
            return [self._to_record(row) for row in rows]

    async def get_stats(
        self, user_id: str | None = None, project_id: str | None = None
    ) -> list[MemoryStatsItem]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.MEMORY_STATS, user_id, project_id)
            return [
                MemoryStatsItem(
                    namespace=row["namespace"],
                    count=row["count"],
                    last_updated=row["last_updated"],
                )
                for row in rows
            ]

    # ════════════════════════════════════════════════════════════
    # UPDATE / SUPERSESSION
    # ════════════════════════════════════════════════════════════

    async def update(
        self,
        memory_id: str,
        metadata: dict | None = None,
        importance: int | None = None,
        project_id: str | None = None,
        confidence: float | None = None,
        frozen: bool | None = None,
        supersedes: str | None = None,
        clear_project_id: bool = False,
    ) -> MemoryRecord | None:
        """Правка обвязки гранулы: metadata merge-ится (dict-merge), version
        бампит триггер БД. Контент НЕ меняется — V3.0 (E.2 ADR-019): путь
        перезаписи отсутствует в слое данных, правка факта = create_version.

        При supersedes закрывает старую гранулу атомарно (одна транзакция):
        status='superseded', valid_to=valid_from новой, superseded_by=memory_id.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    q.UPDATE_MEMORY,
                    memory_id,
                    metadata,
                    importance,
                    project_id,
                    confidence,
                    frozen,
                    clear_project_id,
                    supersedes,
                )
                if row is None:
                    return None
                if supersedes is not None:
                    await conn.fetchrow(q.SUPERSEDE_MEMORY, supersedes, memory_id)
        return self._to_record(row)

    async def create_version(
        self,
        old_id: str,
        content: str,
        metadata: dict | None = None,
        content_hash: str | None = None,
        importance: int | None = None,
        confidence: float | None = None,
    ) -> tuple[MemoryRecord, str] | None:
        """Новая версия гранулы (Фаза 2.1, D3): INSERT-SELECT наследует
        user_id/namespace_id/project_id/version+1/cluster_id из старой строки,
        затем старая закрывается валидным окном (valid_to=valid_from новой),
        затем рёбра старой REWIRE-ом переезжают на наследника (V3.1, дыра 1).

        Одна транзакция: гонка «старая перестала быть asserted между чтением
        и записью» откатывает вставку новой (DatabaseError наружу); REWIRE в
        той же транзакции — рассинхрона «версия есть, рёбра не переехали»
        не существует. Возвращает (новая запись, namespace_id) — id нужен
        фасаду для Qdrant-payload.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                try:
                    inserted = await conn.fetchrow(
                        q.INSERT_MEMORY_VERSION,
                        old_id,
                        content,
                        metadata or {},
                        content_hash,
                        importance,
                        confidence,
                    )
                except asyncpg.exceptions.UniqueViolationError as exc:
                    # Гонка мимо pre-check service: между find_by_content_hash
                    # и INSERT кто-то вклинил тот же active hash — индекс 020
                    # абортирует транзакцию, наружу отдаём доменный конфликт.
                    raise ConflictError(
                        old_id, f"content hash conflict: {exc.constraint_name}"
                    ) from exc
                if inserted is None:
                    return None
                new_id = str(inserted["id"])
                closed = await conn.fetchrow(q.SUPERSEDE_MEMORY, old_id, new_id)
                if closed is None:
                    # INSERT прошёл, но старая уже не asserted → откат всей транзакции
                    raise DatabaseError(
                        f"supersession conflict: granule {old_id} is not asserted"
                    )
                # REWIRE после SUPERSEDE: живость второй стороны оценивается
                # в финальном состоянии (старая уже superseded — её рёбра с
                # живыми сторонами уходят наследнику)
                rewired_src = await conn.fetch(q.REWIRE_RELATIONS_SOURCE, old_id, new_id)
                rewired_tgt = await conn.fetch(q.REWIRE_RELATIONS_TARGET, old_id, new_id)
                record_row = await conn.fetchrow(q.SELECT_MEMORY_BY_ID, new_id)
        rewired = len(rewired_src) + len(rewired_tgt)
        if rewired:
            # Воркер-край операции V3.1: web-INFO tool_handler'а это не дублирует
            RELATIONS_REWIRED_TOTAL.inc(rewired)
            logger.info("create_version: rewired", extra={
                "old_id": old_id, "new_id": new_id, "rewired": rewired,
            })
        return self._to_record(record_row), str(inserted["namespace_id"])

    async def get_history(self, granule_id: str) -> list[MemoryRecord]:
        """Supersession-цепочка (рекурсивный CTE, обе стороны): от старейшей к новейшей."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.GET_HISTORY, granule_id)
            return [self._to_record(row) for row in rows]

    # ════════════════════════════════════════════════════════════
    # DELETE / RETRACT
    # ════════════════════════════════════════════════════════════

    async def delete(self, memory_id: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.DELETE_MEMORY, memory_id)
            return row is not None

    async def archive(self, memory_id: str, reason: str | None = None) -> bool:
        """Отзыв гранулы: status='retracted', valid_to=now(); metadata.reason merge.

        Единый путь retract (Фаза 2.1) — сюда сводятся и разовый отзыв,
        и причина отзыва для аудита.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.RETRACT_MEMORY, memory_id, reason)
            return row is not None

    async def forget_soft(
        self,
        user_id: str,
        namespace_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Мягкое забвение всех гранул пользователя: status='retracted'."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(q.FORGET_MEMORIES, user_id, namespace_id, project_id)

    # ════════════════════════════════════════════════════════════
    # PROJECT CONTEXTS («облачко знаний», D9)
    # ════════════════════════════════════════════════════════════

    async def fetch_project_context(
        self, project_id: str, limit_per_ns: int = 15
    ) -> list[dict]:
        """Топ-гранулы проекта с квотами per namespace (хранимка 019)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.FETCH_PROJECT_CONTEXT, project_id, limit_per_ns)
            return [dict(row) for row in rows]

    async def upsert_project_context(
        self,
        project_id: str,
        content: str | None = None,
        sections: dict | None = None,
        granule_count: int = 0,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.UPSERT_PROJECT_CONTEXT,
                project_id,
                content,
                sections or {},
                granule_count,
            )
            return dict(row)

    async def get_project_context(self, project_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.SELECT_PROJECT_CONTEXT, project_id)
            return dict(row) if row is not None else None

    # ════════════════════════════════════════════════════════════
    # RELATIONS
    # ════════════════════════════════════════════════════════════

    async def add_relation(
        self,
        source_id: str,
        target_id: str | None = None,
        target_name: str | None = None,
        link_type: str = "related_to",
        description: str | None = None,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.INSERT_RELATION,
                source_id,
                target_id,
                target_name,
                link_type,
                description,
                weight,
                metadata or {},
            )
            return str(row["id"])

    async def get_relations_by_source(
        self, source_id: str, link_type: str | None = None
    ) -> list[Relation]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SELECT_RELATIONS_BY_SOURCE, source_id, link_type)
            return [self._to_relation(row) for row in rows]

    async def get_relations_by_target(
        self, target_id: str, link_type: str | None = None
    ) -> list[Relation]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SELECT_RELATIONS_BY_TARGET, target_id, link_type)
            return [self._to_relation(row) for row in rows]

    async def get_relations(
        self, memory_id: str, link_type: str | None = None
    ) -> RelationListResult:
        """Связи + обогащение соседей (Фаза 5.2): для outgoing сосед —
        target, для incoming — source; один батч-SELECT на все концы."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.GET_RELATIONS_UNIFIED, memory_id, link_type)

            outgoing: list[Relation] = []
            incoming: list[Relation] = []
            for row in rows:
                rel = self._to_relation(row)
                if row["direction"] == "outgoing":
                    outgoing.append(rel)
                else:
                    incoming.append(rel)

            neighbor_ids = {rel.target_id for rel in outgoing if rel.target_id} | {
                rel.source_id for rel in incoming
            }
            if neighbor_ids:
                info_rows = await conn.fetch(q.GET_NEIGHBORS_INFO, sorted(neighbor_ids))
                info = {str(row["id"]): row for row in info_rows}
                for rel in outgoing:
                    self._fill_neighbor(rel, info.get(rel.target_id))
                for rel in incoming:
                    self._fill_neighbor(rel, info.get(rel.source_id))
        return RelationListResult(incoming=incoming, outgoing=outgoing)

    @staticmethod
    def _fill_neighbor(rel: Relation, row: asyncpg.Record | None) -> None:
        """Строка GET_NEIGHBORS_INFO → neighbor_* поля связи (нет строки — висячий конец)."""
        if row is None:
            return
        rel.neighbor_namespace = row["namespace"]
        rel.neighbor_entity_name = row["entity_name"]
        rel.neighbor_content = row["content"]
        rel.neighbor_importance = row["importance"]

    async def delete_relation(
        self, source_id: str, target_id: str, link_type: str
    ) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.DELETE_RELATION, source_id, target_id, link_type
            )
            return row is not None

    async def delete_relations_by_source(self, source_id: str) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute(q.DELETE_RELATIONS_BY_SOURCE, source_id)
            return int(result.split()[-1])

    async def find_relations_between(
        self, source_id: str, target_id: str
    ) -> list[Relation]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.FIND_RELATIONS_BETWEEN, source_id, target_id)
            return [self._to_relation(row) for row in rows]

    @staticmethod
    def _to_relation(row: asyncpg.Record) -> Relation:
        return Relation(
            id=str(row["id"]),
            source_id=str(row["source_id"]),
            target_id=str(row["target_id"]) if row["target_id"] else None,
            target_name=row["target_name"],
            link_type=row["link_type"],
            description=row["description"],
            weight=float(row["weight"]),
            metadata=row["metadata"] or {},
            # Guard: старые моки/проекции (GET_RELATIONS_UNIFIED из 020) без
            # колонки происхождения
            inherited_from=(
                str(row["inherited_from"])
                if "inherited_from" in row and row["inherited_from"] is not None
                else None
            ),
            created_at=row["created_at"],
        )

    # ════════════════════════════════════════════════════════════
    # GRAPH
    # ════════════════════════════════════════════════════════════

    async def traverse(
        self, start_id: str, depth: int = 3, link_types: list[str] | None = None
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.TRAVERSE_FULL, start_id, depth, link_types or None)
            if row is None:
                return {"nodes": [], "edges": []}
            return {"nodes": row["nodes"] or [], "edges": row["edges"] or []}

    async def sync_links_to_relations(self, memory_id: str) -> int:
        """Sync metadata.links → relations; lateral-резолв имён внутри SQL (V3.2).

        Возвращает число вставленных/обновлённых рёбер; lateral-резолвы имён
        дополнительно инкрементируют метрику (RETURNING resolved_by_name).
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q.DELETE_SYNCED_RELATIONS, memory_id)
            rows = await conn.fetch(q.BACKFILL_RELATIONS_FROM_METADATA, memory_id)
        self._report_names_resolved(rows)
        return len(rows)

    async def sync_links_batch(self, memory_ids: list[str]) -> int:
        """Batch-sync metadata.links → relations с lateral-резолвом имён (V3.2)."""
        if not memory_ids:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SYNC_LINKS_BATCH, memory_ids)
        self._report_names_resolved(rows)
        return len(rows)

    @staticmethod
    def _report_names_resolved(rows: list) -> None:
        """Метрика имён, разрешённых lateral-резолвом в sync-пути.

        `in` работает и для asyncpg.Record, и для dict (моки тестов).
        """
        resolved = sum(
            1 for r in rows if "resolved_by_name" in r and r["resolved_by_name"]
        )
        if resolved:
            from memory_server.metrics import LINKER_NAMES_RESOLVED_TOTAL

            LINKER_NAMES_RESOLVED_TOTAL.labels(path="sync").inc(resolved)

    async def get_graph_stats(self) -> GraphStats:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q.GRAPH_STATS_UNIFIED)
            total_granules = row["p_total_granules"]
            total_relations = row["p_total_relations"]
            linked_granules = row["p_linked_granules"]
            orphans = row["p_orphans"]
            avg = (total_relations * 2 / total_granules) if total_granules > 0 else 0.0
            return GraphStats(
                total_granules=total_granules,
                total_relations=total_relations,
                linked_granules=linked_granules,
                orphans=orphans,
                avg_connections=round(avg, 2),
                by_namespace=row["p_by_namespace"] or {},
                by_link_type=row["p_by_link_type"] or {},
            )

    # ════════════════════════════════════════════════════════════
    # EDGE LIFECYCLE (V3.5 «Жизнь графа знаний», миграция 026)
    # ════════════════════════════════════════════════════════════

    async def reinforce_relations(
        self, pairs: list[tuple[str, str]], alpha: float, batch: int = 1000
    ) -> int:
        """Боевое касание пар гранул: +1 использование, якорь сейчас, вес
        к 1.0 (α). Пары уже канонизованы вызывающим; батчи по 1000 — один
        round-trip на батч (паттерн «не N+1»). Возвращает число усиленных рёбер.
        """
        touched = 0
        async with self.pool.acquire() as conn:
            for start in range(0, len(pairs), batch):
                chunk = pairs[start : start + batch]
                a_ids = [uuid_module.UUID(a) for a, _ in chunk]
                b_ids = [uuid_module.UUID(b) for _, b in chunk]
                rows = await conn.fetch(q.REINFORCE_RELATIONS, a_ids, b_ids, alpha)
                touched += len(rows)
        return touched

    async def prune_candidates(
        self,
        decay_lambda: float,
        lambda_min: float,
        min_age_days: int,
        floor: float,
    ) -> list[str]:
        """ID кандидатов отсечения (иммунитеты/мост/возраст/raw w_eff — в SQL)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.PRUNE_EDGES_CANDIDATES, decay_lambda, lambda_min, min_age_days, floor
            )
            return [row["id"] for row in rows]

    async def prune_edges_apply(self, edge_ids: list[str], batch: int = 1000) -> int:
        """Отсечение кандидатов: только pruned_at, батчами 1000. Идемпотентно."""
        pruned = 0
        async with self.pool.acquire() as conn:
            for start in range(0, len(edge_ids), batch):
                chunk = edge_ids[start : start + batch]
                rows = await conn.fetch(
                    q.PRUNE_EDGES_APPLY, [uuid_module.UUID(e) for e in chunk]
                )
                pruned += len(rows)
        return pruned

    async def restore_edge(self, edge_id: str, restore_beta: float) -> bool:
        """Ручное воскрешение pruned-ребра (β — восстановительная сила веса)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q.RESTORE_EDGE, uuid_module.UUID(edge_id), restore_beta
            )
            return row is not None

    async def fetch_activation_edges(
        self,
        decay_lambda: float,
        lambda_min: float,
        link_types: list[str] | None = None,
        symmetric_link_types: list[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Живой граф для PPR: (source, target, w_eff) — вес считает SQL
        (ленивая проекция на момент вызова), Python строит только CSR.
        symmetric_link_types — типы, чьи дуги зеркалятся встречной
        (UNION ALL в SQL, вердикт Эны 23.09)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.SELECT_ACTIVATION_EDGES,
                decay_lambda,
                lambda_min,
                link_types or None,
                symmetric_link_types or None,
            )
            return [
                (row["source_id"], row["target_id"], float(row["w_eff"]))
                for row in rows
            ]

    # ════════════════════════════════════════════════════════════
    # LIFECYCLE (Фаза 2.2: decay / stale / GC / orphans)
    # ════════════════════════════════════════════════════════════

    async def decay_confidence(
        self, ns_uids: list[str], rates: list[float], default_rate: float, floor: float
    ) -> dict[str, int]:
        """Ежедневное затухание уверенности — один батч-SQL (Фаза 2.2).

        frozen не трогает SQL; ниже floor не сползает (WHERE confidence > floor).
        Возвращает счётчик затронутых по namespace (RETURNING, без выборки тел).
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.DECAY_CONFIDENCE, ns_uids, rates, default_rate, floor)
        touched: dict[str, int] = {}
        for row in rows:
            ns = row["namespace"]
            touched[ns] = touched.get(ns, 0) + 1
        return touched

    async def count_stale(self, threshold: float, stale_days: int) -> int:
        """Счётчик устаревших кандидатов (status не меняется — only metric)."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(q.COUNT_STALE, threshold, stale_days)

    async def list_stale(
        self,
        threshold: float,
        stale_days: int,
        user_id: str | None = None,
        namespace_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Кандидаты на ревизию для memory_stale_list (динамический критерий)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                q.LIST_STALE, threshold, stale_days, user_id, namespace_id, project_id, limit
            )
            return [self._to_record(row) for row in rows]

    async def select_gc_superseded(self, retention_days: int) -> list[str]:
        """ID superseded-гранул под GC: с наследником и старше retention."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SELECT_GC_SUPERSEDED, retention_days)
            return [row["id"] for row in rows]

    async def delete_gc_superseded(self, granule_ids: list[str]) -> int:
        """Hard delete superseded-гранул + их полностью безадресных связей.

        Одная транзакция: сначала relations без target_name (иначе SET NULL
        оставит ребро без адреса), потом memories (source_id-связи снимет
        CASCADE). Идемпотентно: повтор по пустому списку — no-op.
        """
        if not granule_ids:
            return 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetch(q.DELETE_GC_DANGLING_RELATIONS, granule_ids)
                deleted = await conn.fetch(q.DELETE_GC_SUPERSEDED, granule_ids)
                return len(deleted)

    async def delete_orphan_relations(self) -> int:
        """Связи без адреса целиком (target_id IS NULL AND target_name IS NULL).

        Несуществующих source/target по FK (005: CASCADE/SET NULL) не бывает;
        кластеры member_count=0 — TODO после 022 (таблицы ещё нет).
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.DELETE_ORPHAN_RELATIONS)
            return len(rows)

    # ════════════════════════════════════════════════════════════
    # CLUSTERS (Фаза 2.3, миграция 022 — graceful до её применения)
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _raise_if_schema_pending(exc: Exception) -> None:
        """Undefined* ошибки PG → SchemaPendingError (деградация, не 500)."""
        if isinstance(exc, (asyncpg.exceptions.UndefinedFunctionError, asyncpg.exceptions.UndefinedTableError)):
            raise SchemaPendingError(str(exc)) from exc

    async def fetch_asserted_ids(self, namespace_id: str) -> list[str]:
        """ID актуальных гранул namespace — вход ANN-скролла кластеризации v2.

        Канонический фильтр (status='asserted' AND valid_to IS NULL) —
        те же грани, что у Qdrant-фильтра active_only: пары строятся только
        по живым гранулам, SQL-нормализация хранимки это перепроверяет.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q.SELECT_ASSERTED_CLUSTER_IDS, namespace_id)
            return [row["id"] for row in rows]

    async def apply_cluster_pairs(
        self,
        namespace_id: str,
        pairs: list[tuple[str, str, float]],
        min_members: int = 2,
    ) -> list[dict]:
        """Пары близости → temp _cluster_pairs → assign_clusters_from_pairs (022).

        Одна транзакция: temp-таблица живёт ровно до COMMIT (ON COMMIT DROP),
        COPY не ходит через подготовленные планы — десятки тысяч пар
        льются секундами. Пустой список валиден: хранимка снимет прежнюю
        разметку и почистит опустевшие кластеры (identity-пересчёт).
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(q.CREATE_CLUSTER_PAIRS_TEMP)
                    if pairs:
                        await conn.copy_records_to_table(
                            "_cluster_pairs",
                            records=[
                                (uuid_module.UUID(a), uuid_module.UUID(b), s)
                                for a, b, s in pairs
                            ],
                            columns=("a_id", "b_id", "similarity"),
                        )
                    rows = await conn.fetch(q.REFRESH_CLUSTERS, namespace_id, min_members)
                    return [dict(row) for row in rows]
        except (asyncpg.exceptions.UndefinedFunctionError, asyncpg.exceptions.UndefinedTableError) as exc:
            self._raise_if_schema_pending(exc)

    async def list_clusters(
        self, namespace: str | None = None, project_id: str | None = None
    ) -> list[dict]:
        """Обзор кластеров Level 2 (таблица clusters, миграция 022)."""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(q.LIST_CLUSTERS, namespace, project_id)
                return [dict(row) for row in rows]
        except (asyncpg.exceptions.UndefinedFunctionError, asyncpg.exceptions.UndefinedTableError) as exc:
            self._raise_if_schema_pending(exc)
