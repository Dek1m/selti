from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from memory_server.config import Settings
from memory_server.embedding.provider import EmbeddingProvider
from memory_server.exceptions import (
    ConflictError,
    NotFoundError,
    SchemaPendingError,
    VectorStoreError,
)
from memory_server.logger import async_measure_duration, get_logger
from memory_server.memory.dedup import DedupAction, DedupEngine
from memory_server.memory.namespace_repository import NamespaceRepository
from memory_server.memory.project_repository import ProjectRecord, ProjectRepository
from memory_server.memory.repository import MemoryRepository
from memory_server.memory.search_fusion import (
    final_score,
    importance_weight,
    mmr_rerank,
    recency_decay,
    rrf_fuse,
)
from memory_server.metrics import (
    DEDUP_CONFIRMED_TOTAL,
    GC_PURGE_BLOCKED_TOTAL,
    MEMORIES_VERSIONED_TOTAL,
    RELATIONS_REWIRED_TOTAL,
    ZERO_RESULT_SEARCHES_TOTAL,
)
from memory_server.models import (
    ClusterRecord,
    GraphStats,
    MemoryHistory,
    MemoryListResult,
    MemoryRecord,
    ProjectContext,
    Relation,
    RelationListResult,
    SearchResult,
    TraverseResult,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = get_logger(__name__)

# Redis-ключи облачка (Фаза 6.1): ctx:{slug} — кеш снапшота,
# ctx:{slug}:dirty — флаг «после снапшота были записи в проект».
_CTX_KEY_PREFIX = "ctx:"
_CTX_DIRTY_SUFFIX = ":dirty"

# namespace → секция снапшота; прочие ns — под своим uid (по алфавиту)
_CONTEXT_SECTION_MAP = {
    "project_meta": "decisions",
    "code_knowledge": "code",
    "dialogue_insights": "insights",
    "infrastructure": "infra",
}
_CONTEXT_SECTION_ORDER = ("stack", "decisions", "code", "insights", "infra")

# Квоты секций облачка (смысл как в хранимке 019, но отбор здесь —
# с recency-ранжированием вместо голого importance; решение Мастера 19.09).
_SECTION_QUOTAS = {
    "decisions": 10,
    "code": 15,
    "insights": 5,
    "infra": 5,
}
_SECTION_TITLES = {
    "stack": "Стек",
    "decisions": "Решения",
    "code": "Код",
    "insights": "Инсайты",
    "infra": "Инфраструктура",
}

# content-снапшот — маркдаун-список: гранула = однострочный тезис,
# весь текст ≤100 строк (бюджет additionalContext хука ZCode)
_CONTENT_MAX_LINES = 100
_CONTENT_LINE_MAX_CHARS = 280


def _days_since(moment: datetime | None, now: datetime) -> float:
    """Полных дней с момента (для recency decay); отсутствие момента → 0."""
    if moment is None:
        return 0.0
    return max((now - moment).total_seconds() / 86400.0, 0.0)


def _one_line(text: str, max_chars: int = _CONTENT_LINE_MAX_CHARS) -> str:
    """Гранула → однострочный тезис: переносы схлопываются, хвост обрезается.

    Гарантия «≤100 строк» на content-снапшот: сколько бы ни было абзацев
    в исходной грануле, в списке она занимает одну строку.
    """
    flattened = " ".join(text.split())
    if len(flattened) > max_chars:
        return flattened[: max_chars - 1] + "…"
    return flattened


class MemoryService:
    """Business logic layer for memory operations."""

    def __init__(
        self,
        repository: MemoryRepository,
        embedding_provider: EmbeddingProvider,
        namespace_repository: NamespaceRepository,
        config: Settings | None = None,
        project_repository: ProjectRepository | None = None,
        redis_provider: Callable[[], Awaitable["Redis"]] | None = None,
        linker_dispatch: Callable[[str], None] | None = None,
    ):
        self.repository = repository
        self.embedding = embedding_provider
        self.ns_repo = namespace_repository
        self.project_repo = project_repository
        self.config = config or Settings()
        self.dedup = DedupEngine(repository, embedding_provider, self.config)
        # Ленивая фабрика Redis-клиента (SeltiState.get_redis): кеш облачка
        # не обязателен для корректности — None = деградация в таблицу
        self.redis_provider = redis_provider
        # Диспетчер Линкера V3 (SeltiState → enqueue_link): None = автолинк
        # выключен (юнит-тесты без Celery); вызов best-effort после INSERT
        self.linker_dispatch = linker_dispatch

    async def resolve_project(self, project_id: str | None) -> str | None:
        """Ключ тула (slug | UUID | None) → project_id UUID. Неизвестный slug → NotFoundError."""
        if project_id is None:
            return None
        if self.project_repo is None:
            raise RuntimeError("project_repository is not configured")
        return await self.project_repo.resolve_id(project_id)

    async def _confirm_existing(
        self,
        record: MemoryRecord,
        metadata: dict | None,
        reason: str,
        action: str,
    ) -> MemoryRecord:
        """Confirm-семантика дубля (V3.0, E.3/E.4 ADR-019).

        Повтор факта = подтверждение: metadata merge + confidence recovery
        c' = c + (1−c)×0.1 (cap 1.0, каждое повторение приближает к 1, никогда
        не перескакивая) + bump_access (повтор виден) + sync links (Г3
        ADR-017/019 — раньше не вызывался вовсе). Дешевле старой перезаписи:
        без embedding-записи и Qdrant-синков. Контент никогда не трогается —
        историю потерять невозможно.
        """
        confirmed = await self.repository.update(
            memory_id=record.id,
            metadata={**record.metadata, **(metadata or {})},
            confidence=min(record.confidence + (1.0 - record.confidence) * 0.1, 1.0),
        )
        if confirmed is None:
            raise RuntimeError(f"Failed to confirm memory: {record.id}")
        try:
            await self.repository.bump_access([record.id])
        except Exception:
            logger.exception("store: confirm bump_access FAILED (non-fatal)")
        if metadata and "links" in metadata:
            try:
                synced = await self.repository.sync_links_to_relations(record.id)
                logger.debug("store: confirm sync_links", extra={
                    "synced": synced, "id": record.id,
                })
            except Exception:
                logger.exception(
                    "store: confirm sync_links FAILED (non-fatal)",
                    extra={"id": record.id},
                )
        DEDUP_CONFIRMED_TOTAL.labels(action=action).inc()
        logger.info("store: confirmed existing memory", extra={
            "id": record.id, "reason": reason,
        })
        return confirmed

    async def store(
        self,
        content: str,
        user_id: str,
        metadata: dict | None = None,
        namespace: str | None = None,
        importance: int | None = None,
        project_id: str | None = None,
    ) -> tuple[MemoryRecord, DedupAction]:
        namespace = namespace or "default"
        async with async_measure_duration(logger, "store", namespace=namespace, user_id=user_id):
            ns_record = await self.ns_repo.get_or_create(namespace)
            resolved_project = await self.resolve_project(project_id)
            content_hash: str | None = None
            embedding: list[float] | None = None

            if self.config.dedup_enabled:
                decision = await self.dedup.check(content, user_id, namespace, metadata=metadata)
                content_hash = decision.content_hash
                embedding = decision.embedding
                logger.debug("store: dedup", extra={
                    "action": decision.action.value,
                    "existing_id": decision.existing_id,
                    "score": decision.existing_score,
                })

                if decision.action == DedupAction.SKIP:
                    record = await self.repository.get_by_id(decision.existing_id)
                    if record is None:
                        raise RuntimeError(f"Failed to retrieve existing memory: {decision.existing_id}")
                    # Confirm на SKIP (V3.0, E.4 ADR-019): semantic-дубль — тот
                    # же факт другими словами, повтор подтверждает его так же,
                    # как exact-hash (UPDATE-ветка ниже).
                    confirmed = await self._confirm_existing(
                        record, metadata,
                        reason=f"semantic:{decision.existing_score}",
                        action="skip",
                    )
                    return confirmed, DedupAction.SKIP

                if decision.action == DedupAction.UPDATE:
                    record = await self.repository.get_by_id(decision.existing_id)
                    if record is None:
                        raise RuntimeError(f"Failed to retrieve memory for update: {decision.existing_id}")
                    # Confirm-семантика (V3.0, E.3 ADR-019): exact-hash дубль —
                    # контент байт-в-байт совпал, перезаписывать нечего и нельзя
                    # (история). Повтор факта = подтверждение.
                    confirmed = await self._confirm_existing(
                        record, metadata, reason="exact", action="update"
                    )
                    await self._mark_context_dirty(confirmed.project_id)
                    return confirmed, DedupAction.UPDATE

            if embedding is None:
                embedding = await self.embedding.embed(content)
            memory_id = await self.repository.insert(
                user_id=user_id,
                content=content,
                embedding=embedding,
                metadata=metadata or {},
                namespace_id=ns_record.id,
                content_hash=content_hash,
                importance=importance or 3,
                project_id=resolved_project,
            )
            record = await self.repository.get_by_id(memory_id)
            if record is None:
                raise RuntimeError(f"Failed to retrieve memory after insert: {memory_id}")
            await self._mark_context_dirty(record.project_id)

            if metadata and "links" in metadata:
                try:
                    synced = await self.repository.sync_links_to_relations(memory_id)
                    logger.debug("store: sync_links", extra={"synced": synced, "id": memory_id})
                except Exception:
                    logger.exception("store: sync_links FAILED (non-fatal)", extra={"id": memory_id})

            # Линкер V3: автолинк новой гранулы асинхронно (ADR-019 C —
            # store p95 не меняется; сбой диспетчеризации не роняет запись)
            if self.linker_dispatch is not None:
                try:
                    self.linker_dispatch(memory_id)
                except Exception:
                    logger.exception("store: linker enqueue FAILED (non-fatal)", extra={"id": memory_id})

            return record, DedupAction.INSERT

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
        namespace: str | None = None,
        project_id: str | None = None,
        include_historical: bool = False,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        status: str | None = None,
        offset: int = 0,
    ) -> list[SearchResult]:
        async with async_measure_duration(logger, "search", namespace=namespace, user_id=user_id):
            resolved_project = await self.resolve_project(project_id)
            query_embedding = await self.embedding.embed(query)
            if not self.config.hybrid_search_enabled:
                # Фича-флаг отката (не legacy): выключенный hybrid = плотный
                # Qdrant-путь Фазы 0, документирован в конфиге.
                results = await self.repository.search(
                    query_embedding=query_embedding,
                    user_id=user_id,
                    limit=limit + offset,
                    threshold=threshold,
                    namespace=namespace,
                    query_text=query,
                    project_id=resolved_project,
                    include_historical=include_historical,
                    created_after=created_after,
                    created_before=created_before,
                    status=status,
                )
                results = results[offset:]
            else:
                results = await self._search_hybrid(
                    query=query,
                    query_embedding=query_embedding,
                    user_id=user_id,
                    limit=limit,
                    threshold=threshold,
                    namespace=namespace,
                    project_id=resolved_project,
                    include_historical=include_historical,
                    created_after=created_after,
                    created_before=created_before,
                    status=status,
                    offset=offset,
                )
            if not results:
                # Качество поиска (Фаза 3.3): пустая выдача — сигнал для дашборда
                ZERO_RESULT_SEARCHES_TOTAL.labels(namespace=namespace or "all").inc()
            return results

    async def _search_hybrid(
        self,
        query: str,
        query_embedding: list[float],
        user_id: str | None,
        limit: int,
        threshold: float,
        namespace: str | None,
        project_id: str | None,
        include_historical: bool,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        status: str | None = None,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Hybrid search (Фаза 1.1/1.2): RRF-fusion → MMR → D4-ранжирование.

        offset — пагинация /api/search: пул кандидатов масштабируется до
        offset+limit на канал, слайс делается после полного ранжирования,
        поэтому страницы детерминированы."""
        candidates = await self.repository.search_hybrid(
            query_embedding=query_embedding,
            query_text=query,
            user_id=user_id,
            namespace=namespace,
            project_id=project_id,
            threshold=threshold,
            prefetch=max(self.config.hybrid_prefetch, offset + limit),
            include_historical=include_historical,
            created_after=created_after,
            created_before=created_before,
            status=status,
        )
        if not candidates:
            return []

        rankings = [
            [c.id for c in candidates if c.rank_dense is not None],
            [c.id for c in candidates if c.rank_fts is not None],
        ]
        rrf_scores = rrf_fuse(rankings, k=self.config.rrf_k)
        vectors = {c.id: c.vector for c in candidates if c.vector}
        ordered = mmr_rerank(
            rrf_scores, vectors, top_k=offset + limit, lambda_=self.config.mmr_lambda
        )[offset:]

        now = datetime.now(timezone.utc)
        by_id = {c.id: c for c in candidates}
        results: list[SearchResult] = []
        for cand_id in ordered:
            cand = by_id[cand_id]
            # frozen — вечный факт: не затухает (D4)
            decay = (
                1.0
                if cand.frozen
                else recency_decay(
                    _days_since(cand.last_accessed_at or cand.created_at, now),
                    self.config.recency_decay_rates.get(
                        cand.namespace, self.config.recency_decay_rate
                    ),
                )
            )
            weight = importance_weight(
                cand.importance,
                self.config.importance_multipliers.get(cand.namespace, 1.0),
            )
            results.append(
                SearchResult(
                    id=cand_id,
                    content=cand.content,
                    metadata=cand.metadata,
                    importance=cand.importance,
                    score=round(final_score(rrf_scores[cand_id], decay, weight), 6),
                    project_id=cand.project_id,
                    status=cand.status,
                    namespace=cand.namespace,
                    created_at=cand.created_at,
                    last_accessed_at=cand.last_accessed_at,
                    frozen=cand.frozen,
                    score_rrf=round(rrf_scores[cand_id], 6),
                    score_decay=round(decay, 6),
                    score_importance=round(weight, 6),
                )
            )
        results.sort(key=lambda r: r.score, reverse=True)

        # Инкремент access-полей (Фаза 1.2): батч-UPDATE вне транзакции
        # чтения, только по фактически выданным id; сбой не роняет выдачу.
        try:
            await self.repository.bump_access([r.id for r in results])
        except Exception:
            logger.exception("search: bump_access FAILED (non-fatal)")

        return results

    async def get(self, memory_id: str) -> MemoryRecord:
        async with async_measure_duration(logger, "get"):
            record = await self.repository.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(memory_id)
            return record

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict | None = None,
        importance: int | None = None,
        project_id: str | None = None,
        supersedes: str | None = None,
        clear_project_id: bool = False,
    ) -> MemoryRecord:
        """Обновить гранулу (V3.0, E.1 ADR-019 — честный контракт записи).

        content: НОВАЯ ВЕРСИЯ гранулы — внутренне create_version(reason=
        'edit'): старая строка цела (superseded, окно закрыто), id ответа —
        новая версия, record.supersedes — старый id. Сигнатура сохранена для
        совместимости MCP; история физически не может быть потеряна.
        metadata/importance при content-пути применяются к НОВОЙ версии.
        Без content — правка обвязки на месте: metadata merge, importance,
        project_id, supersedes (легаси-режим закрытия старой).
        """
        async with async_measure_duration(logger, "update"):
            if content is not None:
                return await self.create_version(
                    granule_id=memory_id,
                    new_content=content,
                    metadata_merge={**(metadata or {}), "supersede_reason": "edit"},
                    importance=importance,
                )
            resolved_project = None if clear_project_id else await self.resolve_project(project_id)
            record = await self.repository.update(
                memory_id=memory_id,
                metadata=metadata,
                importance=importance,
                project_id=resolved_project,
                supersedes=supersedes,
                clear_project_id=clear_project_id,
            )
            if record is None:
                raise NotFoundError(memory_id)
            await self._mark_context_dirty(record.project_id)

            if metadata is not None and "links" in metadata:
                try:
                    synced = await self.repository.sync_links_to_relations(memory_id)
                    logger.debug("update: sync_links", extra={"synced": synced, "id": memory_id})
                except Exception:
                    logger.exception("update: sync_links FAILED (non-fatal)", extra={"id": memory_id})

            return record

    async def delete(self, memory_id: str) -> bool:
        async with async_measure_duration(logger, "delete"):
            return await self.repository.delete(memory_id)

    # ── Supersession API (Фаза 2.1, D3) ──

    async def create_version(
        self,
        granule_id: str,
        new_content: str,
        metadata_merge: dict | None = None,
        importance: int | None = None,
    ) -> MemoryRecord:
        """Главная операция факта-конфликта (правило Graphiti): новая версия
        гранулы, старая закрывается её valid_from'ом.

        Наследуются: user/namespace/project/version+1 (SQL), metadata
        (dict-merge), importance (без override). Confidence наследуется
        ×supersession_confidence_factor (cap 0..1) — каждое перепрохождение
        факта стоит части уверенности. Новая версия не frozen (заморозка —
        осознанный ручной акт через freeze).
        """
        async with async_measure_duration(logger, "create_version"):
            old = await self.repository.get_by_id(granule_id)
            if old is None:
                raise NotFoundError(granule_id)
            if old.status != "asserted":
                raise ConflictError(
                    granule_id, f"only asserted granules can be superseded (status={old.status})"
                )
            content_hash = hashlib.sha256(new_content.encode()).hexdigest()
            if content_hash == old.content_hash:
                # unique-индекс idx_memories_content_hash_active увидит обе
                # asserted-строки с одним hash — отсекаем до SQL
                raise ConflictError(granule_id, "new version content is identical to the current one")
            # Чужая активная гранула с тем же hash в namespace: без pre-check
            # INSERT абортируется сырым UniqueViolationError (индекс 020) —
            # отдаём конфликт с id виновника. Свой lookup (hash-рассинхрон
            # старой строки) конфликтом не считается.
            twin = await self.repository.find_by_content_hash(old.namespace, content_hash)
            if twin is not None and twin.id != granule_id:
                raise ConflictError(
                    twin.id, "content is already active in this namespace (exact-dedup)"
                )
            confidence = min(
                max(old.confidence * self.config.supersession_confidence_factor, 0.0),
                1.0,
            )
            embedding = await self.embedding.embed(new_content)
            record = await self.repository.create_version(
                old_id=granule_id,
                content=new_content,
                embedding=embedding,
                metadata={**old.metadata, **(metadata_merge or {})},
                content_hash=content_hash,
                importance=importance,
                confidence=confidence,
            )
            if record is None:
                raise NotFoundError(granule_id)
            # Версия наследует project старой гранулы — снапшот устарел
            await self._mark_context_dirty(old.project_id)
            logger.info("create_version: versioned", extra={
                "old_id": granule_id, "new_id": record.id,
                "confidence": round(confidence, 3),
                "reason": (metadata_merge or {}).get("supersede_reason", "explicit"),
            })
            MEMORIES_VERSIONED_TOTAL.labels(
                reason=(metadata_merge or {}).get("supersede_reason", "explicit")
            ).inc()
            return record

    async def get_history(self, granule_id: str) -> MemoryHistory:
        """Вся supersession-цепочка гранулы: от старейшей к новейшей, текущая помечена."""
        async with async_measure_duration(logger, "get_history"):
            start = await self.repository.get_by_id(granule_id)
            if start is None:
                raise NotFoundError(granule_id)
            items = await self.repository.get_history(granule_id)
            current = next(
                (r for r in items if r.status == "asserted" and r.valid_to is None), None
            )
            return MemoryHistory(
                items=items,
                current_id=current.id if current else None,
            )

    async def retract(self, memory_id: str, reason: str | None = None) -> bool:
        """Отзыв гранулы: status='retracted', valid_to=now(), metadata.reason.

        Единый путь retract для всех тулов (разовый memory_archive и
        причина отзыва для аудита).
        """
        logger.debug("retract", extra={"id": memory_id, "reason": reason})
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            logger.debug("retract: not found", extra={"id": memory_id})
            raise NotFoundError(memory_id)
        await self._mark_context_dirty(record.project_id)
        result = await self.repository.archive(memory_id, reason=reason)
        logger.debug("retract: done", extra={"id": memory_id, "success": result})
        return result

    async def freeze(self, memory_id: str, frozen: bool) -> MemoryRecord:
        """Ручная заморозка вечных фактов (D4): защита от decay и GC.

        Замороженные не затухают (SQL decay фильтрует frozen) — вечные
        факты не требуют периодического подтверждения.
        """
        async with async_measure_duration(logger, "freeze"):
            record = await self.repository.update(memory_id=memory_id, frozen=frozen)
            if record is None:
                raise NotFoundError(memory_id)
            return record

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
        project_id: str | None = None,
    ) -> MemoryListResult:
        async with async_measure_duration(logger, "list", namespace=namespace):
            resolved_project = await self.resolve_project(project_id)
            return await self.repository.list(
                user_id=user_id,
                namespace=namespace,
                limit=limit,
                offset=offset,
                project_id=resolved_project,
            )

    async def recent(
        self,
        namespace: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[MemoryRecord]:
        logger.debug("recent", extra={"namespace": namespace, "limit": limit, "since": str(since)})
        resolved_project = await self.resolve_project(project_id)
        results = await self.repository.recent(
            namespace=namespace,
            since=since,
            limit=limit,
            project_id=resolved_project,
        )
        logger.debug("recent: done", extra={"count": len(results)})
        return results

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Забвение гранул пользователя (retract): опционально namespace и проект.

        project_id (slug/UUID, Фаза 3.1) ограничивает забвение гранулами
        проекта — глобальный слой юзера не трогаем.
        """
        logger.debug("forget", extra={
            "user_id": user_id, "namespace": namespace, "project_id": project_id,
        })
        resolved_project = await self.resolve_project(project_id)
        count = await self.repository.forget(
            user_id=user_id,
            namespace=namespace,
            project_id=resolved_project,
        )
        logger.debug("forget: done", extra={"deleted_count": count})
        return count

    async def get_stats(
        self, user_id: str | None = None, project_id: str | None = None
    ) -> list:
        """Статистика по namespace; project_id (slug/UUID) — срез по проекту."""
        logger.debug("get_stats", extra={"user_id": user_id, "project_id": project_id})
        resolved_project = await self.resolve_project(project_id)
        result = await self.repository.get_stats(user_id, project_id=resolved_project)
        logger.debug("get_stats: done", extra={"namespaces": len(result)})
        return result

    async def archive(self, memory_id: str) -> bool:
        """Разовый отзыв без причины — сводится на единый путь retract (Фаза 2.1)."""
        return await self.retract(memory_id)

    # ── Жизненный цикл (Фаза 2.2): decay / stale / GC / orphans ──

    async def decay_confidence(self) -> dict[str, int]:
        """Ежедневное затухание уверенности: батч-SQL, per-namespace rate.

        rates берутся из config.recency_decay_rates (единый источник
        скоростей затухания с ранжированием D4); метрика — счёт по namespace.
        """
        rates = self.config.recency_decay_rates
        touched = await self.repository.decay_confidence(
            ns_uids=list(rates.keys()),
            rates=list(rates.values()),
            default_rate=self.config.recency_decay_rate,
            floor=self.config.confidence_decay_floor,
        )
        logger.info("decay_confidence: done", extra={"touched_total": sum(touched.values())})
        return touched

    async def mark_stale(self) -> int:
        """Счётчик устаревших кандидатов (статус НЕ меняется — ревизия ручная).

        Кандидат: asserted, confidence < stale_threshold, нет доступа
        дольше stale_days. Warning-лог — сигнал для memory_stale_list.
        """
        count = await self.repository.count_stale(
            self.config.stale_threshold, self.config.stale_days
        )
        if count:
            logger.warning("mark_stale: candidates for revision", extra={"count": count})
        return count

    async def stale_list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Кандидаты на ревизию (динамический запрос, колонки-флага нет)."""
        namespace_id = await self._ns_id_or_none(namespace)
        resolved_project = await self.resolve_project(project_id)
        return await self.repository.list_stale(
            threshold=self.config.stale_threshold,
            stale_days=self.config.stale_days,
            user_id=user_id,
            namespace_id=namespace_id,
            project_id=resolved_project,
            limit=limit,
        )

    async def gc_superseded(self) -> dict[str, int | str | bool]:
        """GC закрытых версий (еженедельно): superseded с наследником и старше
        gc_retention_days.

        Стоп-кран V3.1 (F ADR-019, дыра 7): полная история = purge выключен.
          * gc_purge_enabled=False (мастер-кран, дефолт) — НИКОГДА не удаляем,
            какие бы mode ни стояли;
          * gc_mode='disabled' (дефолт) — то же: только счётчик кандидатов;
          * gc_mode='hard' + gc_purge_enabled=True — hard delete как раньше.
        Beat продолжает отчитывать selected — наблюдаемость без действия.
        """
        purge_allowed = self.config.gc_purge_enabled and self.config.gc_mode == "hard"
        ids = await self.repository.select_gc_superseded(self.config.gc_retention_days)
        result: dict[str, int | str | bool] = {
            "mode": self.config.gc_mode,
            "purge_enabled": self.config.gc_purge_enabled,
            "selected": len(ids),
            "deleted": 0,
        }
        if not ids:
            return result
        if not purge_allowed:
            GC_PURGE_BLOCKED_TOTAL.labels(
                reason="purge_disabled" if not self.config.gc_purge_enabled
                else "mode_disabled"
            ).inc()
            logger.warning("gc_superseded: purge disabled, candidates only", extra={
                "selected": len(ids),
                "mode": self.config.gc_mode,
                "purge_enabled": self.config.gc_purge_enabled,
                "retention_days": self.config.gc_retention_days,
            })
            return result
        deleted = await self.repository.purge_memories(ids)
        logger.info("gc_superseded: deleted", extra={"selected": len(ids), "deleted": deleted})
        result["deleted"] = deleted
        return result

    async def orphans_cleanup(self) -> int:
        """Связи без адреса целиком (после SET NULL от GC). Идемпотентно."""
        removed = await self.repository.delete_orphan_relations()
        logger.info("orphans_cleanup: done", extra={"removed": removed})
        return removed

    # ── Кластеризация Level 2 (Фаза 2.3, миграция 022) ──

    async def refresh_clusters(self, namespace: str) -> dict:
        """Пересчёт кластеров namespace (v2: кандидаты — Qdrant ANN, 022).

        Graceful-отказы (beat-расписание не ломается, retry поднимет повтор):
          * миграция 022 не применена → ok=False "migration 022 pending";
          * Qdrant недоступен → ok=False "qdrant_unavailable": пары собрать
            нельзя, прежняя разметка кластеров НЕ трогается.
        """
        ns_record = await self.ns_repo.get_by_uid(namespace)
        if ns_record is None:
            raise NotFoundError(namespace, message=f"namespace is not registered: {namespace}")
        try:
            rows = await self.repository.refresh_clusters(
                ns_record.id,
                threshold=self.config.cluster_threshold,
                top_k=self.config.cluster_top_k,
                min_members=self.config.cluster_min_members,
            )
        except SchemaPendingError:
            logger.warning(
                "refresh_clusters: assign_clusters_from_pairs not available "
                "(migration 022 pending)"
            )
            return {"ok": False, "reason": "migration 022 pending", "clusters": []}
        except VectorStoreError as exc:
            logger.warning(
                "refresh_clusters: qdrant unavailable, clustering skipped",
                extra={"namespace": namespace, "error": str(exc)},
            )
            return {"ok": False, "reason": "qdrant_unavailable", "clusters": []}
        logger.info("refresh_clusters: done", extra={"namespace": namespace, "clusters": len(rows)})
        return {"ok": True, "clusters": rows}

    async def cluster_list(
        self, namespace: str | None = None, project_id: str | None = None
    ) -> dict:
        """Обзор кластеров Level 2. Graceful до миграции 022."""
        resolved_project = await self.resolve_project(project_id)
        try:
            rows = await self.repository.list_clusters(namespace, resolved_project)
        except SchemaPendingError:
            logger.warning("cluster_list: clusters table not available (migration 022 pending)")
            return {"ok": False, "reason": "migration 022 pending", "clusters": []}
        return {
            "ok": True,
            "clusters": [
                ClusterRecord(
                    id=row["id"],
                    namespace=row["namespace"],
                    label=row.get("label"),
                    summary=row.get("summary"),
                    member_count=row.get("member_count", 0),
                    coherence=row.get("coherence"),
                    last_computed_at=row.get("last_computed_at"),
                ).model_dump(mode="json")
                for row in rows
            ],
        }

    async def _ns_id_or_none(self, namespace: str | None) -> str | None:
        """uid → namespace_id (None-вход → None-фильтр); для lifecycle-запросов."""
        if namespace is None:
            return None
        record = await self.ns_repo.get_by_uid(namespace)
        return record.id if record else None

    # ── Project context («облачко знаний», D9; Фаза 6: кеш + dirty) ──

    async def _get_redis(self) -> "Redis | None":
        """Redis-клиент процесса или None (кеш облачка опционален)."""
        if self.redis_provider is None:
            return None
        try:
            return await self.redis_provider()
        except Exception:
            logger.warning("context: redis unavailable, degrading to table-only")
            return None

    async def _mark_context_dirty(self, project_id: str | UUID | None) -> None:
        """Флаг «снапшот проекта устарел» после записи/правки гранулы.

        Best-effort: сбой Redis/реестра не роняет основную операцию —
        пересборка по beat всё равно догонит по TTL.
        """
        if project_id is None:
            return
        try:
            redis = await self._get_redis()
            if redis is None:
                return
            record = await self.project_repo.get_by_id(str(project_id))
            if record is None:
                return
            await redis.set(
                f"{_CTX_KEY_PREFIX}{record.slug}{_CTX_DIRTY_SUFFIX}",
                "1",
                ex=self.config.context_cache_ttl,
            )
            logger.debug("context: dirty", extra={"slug": record.slug})
        except Exception:
            logger.warning(
                "context: mark dirty FAILED (non-fatal)",
                extra={"project_id": str(project_id)},
            )

    async def _is_dirty(self, redis: "Redis", slug: str) -> bool:
        try:
            return bool(
                await redis.exists(f"{_CTX_KEY_PREFIX}{slug}{_CTX_DIRTY_SUFFIX}")
            )
        except Exception:
            return False

    async def _require_project_record(self, project: str) -> ProjectRecord:
        """slug | UUID → карточка проекта (для кеша нужен slug, не только id)."""
        if self.project_repo is None:
            raise RuntimeError("project_repository is not configured")
        record = await self.project_repo.get_by_slug(project)
        if record is not None:
            return record
        # Не slug реестра: единственная допустимая альтернатива — готовый UUID
        try:
            UUID(project)
        except ValueError:
            raise NotFoundError(
                project,
                message=f"Project not found: '{project}' (neither slug in registry nor UUID)",
            ) from None
        record = await self.project_repo.get_by_id(project)
        if record is not None:
            return record
        raise NotFoundError(project, message=f"Project not found: '{project}'")

    async def get_project_context(
        self, project: str, refresh: bool = False
    ) -> ProjectContext:
        """Снапшот контекста проекта: Redis ctx:{slug} → таблица → пересчёт.

        Fast-path без Celery-задач пересборки. dirty-флаг не блокирует
        выдачу — снапшот отдаётся как есть с полем stale=True (честность
        без штрафа на чтение); refresh=True пересчитывает немедленно.
        """
        record = await self._require_project_record(project)
        if refresh:
            return await self._rebuild_context(record)

        redis = await self._get_redis()
        if redis is not None:
            try:
                raw = await redis.get(f"{_CTX_KEY_PREFIX}{record.slug}")
            except Exception:
                raw = None
                logger.warning(
                    "context: cache read failed, treating as miss",
                    extra={"slug": record.slug},
                )
            if raw is not None:
                try:
                    context = ProjectContext.model_validate(json.loads(raw))
                except Exception:
                    # Битый кеш = промах: таблица/пересчёт исправят
                    context = None
                if context is not None:
                    context.stale = await self._is_dirty(redis, record.slug)
                    return context

        row = await self.repository.get_project_context(record.id)
        if row is None:
            return await self._rebuild_context(record)
        context = ProjectContext.model_validate(row)
        if redis is not None:
            context.stale = await self._is_dirty(redis, record.slug)
            await self._cache_context(record.slug, context)
        return context

    async def rebuild_project_context(self, project: str) -> ProjectContext:
        """Пересчитать снапшот из топ-гранул проекта (хранимка 020 + стек 017)."""
        record = await self._require_project_record(project)
        return await self._rebuild_context(record)

    async def rebuild_dirty_contexts(self) -> dict:
        """Beat-пересборка (почасово): проекты с dirty-флагом → rebuild.

        Кандидаты — реестр (проектов единицы) с точечным GET флага:
        без KEYS/SCAN по чужому ключевому пространству Redis.
        """
        redis = await self._get_redis()
        projects = await self.project_repo.list_all()
        rebuilt: list[str] = []
        for record in projects:
            if redis is not None and await self._is_dirty(redis, record.slug):
                await self._rebuild_context(record)
                rebuilt.append(record.slug)
        logger.info(
            "rebuild_dirty_contexts: done",
            extra={"scanned": len(projects), "rebuilt": len(rebuilt)},
        )
        return {"scanned": len(projects), "rebuilt": rebuilt}

    async def _cache_context(self, slug: str, context: ProjectContext) -> None:
        """Снапшот в Redis + снять dirty. Best-effort — сбой не роняет путь."""
        redis = await self._get_redis()
        if redis is None:
            return
        try:
            await redis.set(
                f"{_CTX_KEY_PREFIX}{slug}",
                context.model_dump_json(),
                ex=self.config.context_cache_ttl,
            )
            await redis.delete(f"{_CTX_KEY_PREFIX}{slug}{_CTX_DIRTY_SUFFIX}")
        except Exception:
            logger.warning(
                "context: cache write failed (non-fatal)", extra={"slug": slug}
            )

    async def _rebuild_context(self, record: ProjectRecord) -> ProjectContext:
        """Механическая сборка снапшота (fallback прозы Тиши, план 6.2).

        Секции: стек (project_technologies/project_links, 017) + топ-гранулы
        по namespace. Кандидаты — pg.list по (namespace, project), ранжирование
        Python-ом: importance × recency_decay (свежее поднимается, древнее
        importance-5 тонет — решение Мастера 19.09; хранимка 019 сортирует
        только по importance и тянет вечных чемпионов).
        content — маркдаун-список ≤100 строк: гранула → однострочный тезис.
        """
        sections: dict[str, list[str]] = {}
        stack_lines = self._stack_lines(await self.project_repo.fetch_stack(record.id))
        if stack_lines:
            sections["stack"] = stack_lines

        granule_count = 0
        for uid, section in _CONTEXT_SECTION_MAP.items():
            result = await self.repository.list(
                namespace=uid, project_id=record.id, limit=40
            )
            ranked = self._rank_candidates(
                result.items, self.config.cloud_recency_half_life_days
            )
            quota = _SECTION_QUOTAS.get(section, 5)
            sections[section] = [r.content for r in ranked[:quota]]
            granule_count += min(len(ranked), quota)

        content = self._render_content(record, sections)
        saved = await self.repository.upsert_project_context(
            record.id,
            content=content,
            sections=sections,
            granule_count=granule_count,
        )
        context = ProjectContext(
            project_id=record.id,
            content=content,
            sections=sections,
            granule_count=granule_count,
            computed_at=saved.get("computed_at"),
        )
        await self._cache_context(record.slug, context)
        return context

    @staticmethod
    def _rank_candidates(items, half_life_days: float) -> list:
        """Кандидаты секции → отсортированные по score = importance × decay^дней.

        Период полураспада (cloud_recency_half_life_days, дефолт 30):
        за месяц важность гранулы теряет половину веса — древние чемпионы
        тонут, свежие решения всплывают. frozen не затухает. Обрезка
        тезиса — на границе предложения, ≤300.
        """
        now = datetime.now(timezone.utc)

        def _score(item) -> float:
            updated = item.updated_at or item.created_at or now
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            days = max(0.0, (now - updated).total_seconds() / 86400)
            decay = 1.0 if item.frozen else 0.5 ** (days / half_life_days)
            return (item.importance or 3) * decay

        ranked = sorted(items, key=_score, reverse=True)

        def _teaser(text: str) -> str:
            text = (text or "").strip()
            if len(text) <= 300:
                return text
            cut = text[:300]
            dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            return (cut[: dot + 1] if dot > 120 else cut) + "…"

        for item in ranked:
            item.content = _teaser(item.content)
        return ranked

    @staticmethod
    def _stack_lines(stack: dict) -> list[str]:
        """Стек проекта → строки секции: «PostgreSQL 16 — основная БД»,
        «repo: https://… — заголовок»."""
        lines: list[str] = []
        for tech in stack.get("technologies", []):
            entry = tech["name"] + (f" {tech['version']}" if tech.get("version") else "")
            if tech.get("purpose"):
                entry += f" — {tech['purpose']}"
            lines.append(entry)
        for link in stack.get("links", []):
            title = f" — {link['title']}" if link.get("title") else ""
            lines.append(f"{link['link_type']}: {link['url']}{title}")
        return lines

    @staticmethod
    def _render_content(record: ProjectRecord, sections: dict) -> str:
        """Секции → читаемый маркдаун: канонический порядок, cap 100 строк."""
        ordered = [s for s in _CONTEXT_SECTION_ORDER if s in sections]
        ordered += sorted(s for s in sections if s not in _CONTEXT_SECTION_ORDER)
        parts: list[str] = [f"# {record.name} — облачко знаний"]
        for section in ordered:
            parts.append(f"## {_SECTION_TITLES.get(section, section)}")
            parts.extend(f"- {_one_line(item)}" for item in sections[section])
        if len(parts) <= _CONTENT_MAX_LINES:
            return "\n".join(parts)
        trimmed = parts[:_CONTENT_MAX_LINES]
        trimmed[-1] = "… (обрезано по лимиту 100 строк)"
        return "\n".join(trimmed)

    # ── Relations ──

    async def _resolve_granule(self, granule_id: str) -> MemoryRecord | None:
        """Найти гранулу: сначала по UUID, потом по entity_name (fallback)."""
        # Пробуем UUID
        try:
            record = await self.repository.get_by_id(granule_id)
            if record is not None:
                return record
        except Exception as exc:
            # Ожидаемо для не-UUID входа, но сюда же попадает сбой БД — не молчим
            logger.warning("resolve_granule: uuid lookup failed, fallback to entity_name", extra={
                "input": granule_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })

        # Fallback: ищем по entity_name
        record = await self.repository.find_by_entity_name(granule_id)
        if record is not None:
            logger.debug("resolve: found by entity_name", extra={
                "input": granule_id, "resolved_id": record.id,
            })
            return record

        logger.warning("resolve_granule: not found by uuid nor entity_name", extra={
            "input": granule_id,
        })
        return record

    async def add_relation(
        self,
        source_id: str,
        target_id: str | None = None,
        target_name: str | None = None,
        link_type: str = "related_to",
        description: str | None = None,
        weight: float = 1.0,
        metadata: dict | None = None,
        project_id: str | None = None,
    ) -> str | None:
        """Создать связь между гранулами. Поддерживает строковые entity_name как ID.

        project_id — только ранняя валидация проекта (понятная ошибка при
        неизвестном slug); связи фильтром проекта не ограничены.
        """
        logger.debug("add_relation", extra={
            "source": source_id, "target": target_id,
            "type": link_type, "weight": weight,
        })
        if project_id is not None:
            await self.resolve_project(project_id)

        # Resolve source
        source = await self._resolve_granule(source_id)
        if source is None:
            logger.warning("add_relation: source not found", extra={"source": source_id})
            return None
        resolved_source_id = source.id

        # Resolve target
        resolved_target_id: str | None = None
        if target_id is not None:
            target = await self._resolve_granule(target_id)
            if target is None:
                logger.warning("add_relation: target not found", extra={"target": target_id})
                return None
            resolved_target_id = target.id

        rel_id = await self.repository.add_relation(
            source_id=resolved_source_id,
            target_id=resolved_target_id,
            target_name=target_name,
            link_type=link_type,
            description=description,
            weight=weight,
            metadata=metadata,
        )
        logger.debug("add_relation: done", extra={"relation_id": rel_id})
        return rel_id

    async def get_relations(
        self, memory_id: str, link_type: str | None = None
    ) -> RelationListResult:
        """Получить входящие и исходящие связи гранулы. Один запрос вместо двух."""
        logger.debug("get_relations", extra={"id": memory_id, "link_type": link_type})
        result = await self.repository.get_relations(memory_id, link_type)
        logger.debug("get_relations: done", extra={"incoming": len(result.incoming), "outgoing": len(result.outgoing)})
        return result

    async def delete_relation(
        self, source_id: str, target_id: str, link_type: str
    ) -> bool:
        """Удалить связь."""
        logger.debug("delete_relation", extra={
            "source": source_id, "target": target_id, "type": link_type,
        })
        result = await self.repository.delete_relation(source_id, target_id, link_type)
        logger.debug("delete_relation: done", extra={"success": result})
        return result

    async def traverse(
        self,
        start_id: str,
        depth: int = 3,
        link_types: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        project_id: str | None = None,
    ) -> TraverseResult:
        """Обход графа от начальной ноды. Один round-trip вместо 2N+2.

        Cap traverse_max_nodes + курсорная пагинация — Python-слой поверх
        хранимки graph_traverse_full (Фаза 1.5, миграций нет): стабильный
        порядок сортировкой id, срез [offset : offset+limit], рёбра —
        только между выданными узлами. total_nodes/truncated — навигация.

        project_id — только ранняя валидация проекта (понятная ошибка при
        неизвестном slug, паттерн memory_link): граф связей глобальный,
        фильтра узлов по проекту в контракте хранимки нет.
        """
        logger.debug("traverse", extra={
            "start_id": start_id, "depth": depth, "link_types": link_types,
            "limit": limit, "offset": offset, "project_id": project_id,
        })
        await self.resolve_project(project_id)
        # Валидация: start_id должен существовать
        start = await self.repository.get_by_id(start_id)
        if start is None:
            raise NotFoundError(f"Start granule: {start_id}")
        raw = await self.repository.traverse(start_id, depth, link_types)

        all_nodes = sorted(raw["nodes"], key=lambda n: str(n["id"]))
        total = len(all_nodes)
        capped = all_nodes[: self.config.traverse_max_nodes]
        page = capped[offset : offset + limit] if limit is not None else capped[offset:]
        visible_ids = {str(n["id"]) for n in page}

        edges = [
            Relation(
                id=str(e["id"]),
                source_id=str(e["source_id"]),
                target_id=str(e["target_id"]) if e.get("target_id") else None,
                link_type=e["link_type"],
                description=e.get("description"),
                weight=float(e.get("weight", 1.0)),
                metadata=e.get("metadata", {}),
            )
            for e in raw["edges"]
            # Подграф из выданных узлов: soft-resolve связи (target_name-only)
            # сохраняем — они привязаны к видимому источнику
            if str(e["source_id"]) in visible_ids
            and (e.get("target_id") is None or str(e["target_id"]) in visible_ids)
        ]
        logger.debug("traverse: done", extra={
            "nodes": len(page), "edges": len(edges), "total_nodes": total,
        })
        return TraverseResult(
            nodes=page,
            edges=edges,
            total_nodes=total,
            truncated=len(page) < total,
        )

    async def get_graph_stats(self) -> GraphStats:
        """Статистика графа знаний."""
        logger.debug("get_graph_stats")
        result = await self.repository.get_graph_stats()
        logger.debug("get_graph_stats: done", extra={
            "granules": result.total_granules,
            "relations": result.total_relations,
            "orphans": result.orphans,
        })
        return result
