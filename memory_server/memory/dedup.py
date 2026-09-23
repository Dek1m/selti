import asyncio
import hashlib
from dataclasses import dataclass
from enum import Enum

from memory_server.embedding.provider import EmbeddingProvider
from memory_server.runtime_config import RuntimeConfig
from memory_server.logger import get_logger
from memory_server.memory.repository import MemoryRepository
from memory_server.metrics import DEDUP_SKIPPED_TOTAL, DEDUP_INSERTED_TOTAL, DEDUP_RATIO
from memory_server.models import SearchResult

logger = get_logger(__name__)

# Бегущие счётчики для вычисления dedup ratio (per-process).
# Корректно в single-worker; в multiprocess — приближение (достаточно для dashboards).
_dedup_counts: dict[str, dict[str, int]] = {}


class DedupAction(Enum):
    INSERT = "insert"
    SKIP = "skip"
    UPDATE = "update"


@dataclass
class DedupDecision:
    action: DedupAction
    existing_id: str | None = None
    existing_score: float | None = None
    content_hash: str | None = None
    embedding: list[float] | None = None  # кэш эмбеддинга от dedup check


class DedupEngine:
    def __init__(
        self,
        repository: MemoryRepository,
        embedding_client: EmbeddingProvider,
        runtime: RuntimeConfig,
    ):
        self.repository = repository
        self.embedding = embedding_client
        self.runtime = runtime

    @staticmethod
    def _update_ratio(namespace: str, action: DedupAction) -> None:
        """Обновить DEDUP_RATIO gauge после каждого dedup-решения."""
        if namespace not in _dedup_counts:
            _dedup_counts[namespace] = {"skipped": 0, "inserted": 0}
        counts = _dedup_counts[namespace]
        if action in (DedupAction.SKIP, DedupAction.UPDATE):
            counts["skipped"] += 1
        elif action == DedupAction.INSERT:
            counts["inserted"] += 1
        total = counts["skipped"] + counts["inserted"]
        if total > 0:
            DEDUP_RATIO.labels(namespace=namespace).set(counts["skipped"] / total)

    def _semantic_match(
        self,
        results: list[SearchResult],
        threshold: float,
        incoming_metadata: dict | None,
    ) -> SearchResult | None:
        """Топ-результат выше порога и entity_name не конфликтует → дубль.

        Сравнение скоуплено namespace (search вызывается с namespace-фильтром);
        пары сущностей cross-namespace — TODO Фазы 4 (D7/research).
        """
        if not results or results[0].score < threshold:
            return None
        incoming_entity = (incoming_metadata or {}).get("entity_name", "").strip().lower()
        existing_entity = (results[0].metadata or {}).get("entity_name", "").strip().lower()
        if incoming_entity and existing_entity and incoming_entity != existing_entity:
            return None  # не дубль: entity_name разный, гранулы дополняют друг друга
        return results[0]

    async def check(
        self,
        content: str,
        user_id: str,
        namespace: str = "default",
        metadata: dict | None = None,
    ) -> DedupDecision:
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        if not self.runtime.get("dedup_enabled"):
            logger.info("Dedup disabled — force INSERT", extra={
                "namespace": namespace, "hash": content_hash[:16],
            })
            self._update_ratio(namespace, DedupAction.INSERT)
            return DedupDecision(action=DedupAction.INSERT, content_hash=content_hash)

        # Exact dedup
        existing = await self.repository.find_by_content_hash(namespace, content_hash)
        if existing is not None:
            action = DedupAction.UPDATE if namespace == "user_facts" else DedupAction.SKIP
            logger.info("Exact dedup match", extra={
                "namespace": namespace, "action": action.value, "id": existing.id,
            })
            DEDUP_SKIPPED_TOTAL.labels(namespace=namespace, reason="exact").inc()
            self._update_ratio(namespace, action)
            return DedupDecision(
                action=action,
                existing_id=existing.id,
                content_hash=content_hash,
            )

        # Semantic dedup
        threshold = self.runtime.get("dedup_thresholds").get(namespace, self.runtime.get("dedup_threshold"))
        vector = await self.embedding.embed(content)
        results = await self.repository.search(
            query_embedding=vector,
            user_id=user_id,
            namespace=namespace,
            threshold=threshold,
            limit=5,
        )

        best = self._semantic_match(results, threshold, metadata)
        if best is not None:
            logger.info("Semantic dedup match", extra={
                "namespace": namespace, "score": best.score, "id": best.id,
            })
            DEDUP_SKIPPED_TOTAL.labels(namespace=namespace, reason="semantic").inc()
            self._update_ratio(namespace, DedupAction.SKIP)
            return DedupDecision(
                action=DedupAction.SKIP,
                existing_id=best.id,
                existing_score=best.score,
                content_hash=content_hash,
            )

        DEDUP_INSERTED_TOTAL.labels(namespace=namespace).inc()
        self._update_ratio(namespace, DedupAction.INSERT)
        logger.info("Dedup INSERT", extra={"namespace": namespace, "hash": content_hash[:16]})
        return DedupDecision(
            action=DedupAction.INSERT,
            content_hash=content_hash,
            embedding=vector,
        )

    async def check_batch(
        self,
        entries: list[dict],
        user_id: str,
    ) -> list[DedupDecision]:
        """Batch dedup: batch exact lookup → intra-batch dedup → batch embedding → параллельный semantic.

        Оптимизации Фазы 1.4: exact-фаза — ОДИН запрос на весь батч
        (пары (uid, hash) через unnest) вместо цикла; semantic-фаза —
        asyncio.gather вместо serial (Qdrant-клиент за CircuitBreaker —
        отказ одного поиска не роняет батч, INSERT-fallback).
        """
        if not self.runtime.get("dedup_enabled"):
            return [
                DedupDecision(
                    action=DedupAction.INSERT,
                    content_hash=hashlib.sha256(e["content"].encode()).hexdigest(),
                )
                for e in entries
            ]

        # Phase 1: content hashes для всех entries (CPU-only, мгновенно)
        hashes = [hashlib.sha256(e["content"].encode()).hexdigest() for e in entries]

        # Phase 2: exact dedup — один batch-запрос пар (uid, hash)
        ns_groups: dict[str, list[tuple[int, str]]] = {}
        for i, (entry, h) in enumerate(zip(entries, hashes)):
            ns = entry.get("namespace", "default")
            ns_groups.setdefault(ns, []).append((i, h))

        pairs = [(ns, h) for ns, items in ns_groups.items() for _, h in items]
        found = (
            await self.repository.find_by_content_hashes(
                [ns for ns, _ in pairs], [h for _, h in pairs]
            )
            if pairs
            else {}
        )

        decisions: list[DedupDecision | None] = [None] * len(entries)
        for ns, items in ns_groups.items():
            for idx, h in items:
                existing = found.get((ns, h))
                if existing is not None:
                    action = DedupAction.UPDATE if ns == "user_facts" else DedupAction.SKIP
                    DEDUP_SKIPPED_TOTAL.labels(namespace=ns, reason="exact").inc()
                    self._update_ratio(ns, action)
                    decisions[idx] = DedupDecision(
                        action=action,
                        existing_id=existing.id,
                        content_hash=h,
                    )

        # Phase 2.5: intra-batch exact dedup. Повторный (namespace, hash) внутри
        # одного батча не виден exact-фазе выше (той нужны строки В БД) — без
        # этой проверки обе записи получают INSERT и падают на
        # idx_memories_content_hash_active целиком откатывая батч (прод-инцидент).
        # existing_id=None: id первого вхождения ещё не существует — consumer
        # проставит его после вставки (ingest_batch).
        seen_in_batch: dict[tuple[str, str], int] = {}
        for i, (entry, h) in enumerate(zip(entries, hashes)):
            if decisions[i] is not None:
                continue
            ns = entry.get("namespace", "default")
            first_idx = seen_in_batch.setdefault((ns, h), i)
            if first_idx == i:
                continue
            action = DedupAction.UPDATE if ns == "user_facts" else DedupAction.SKIP
            DEDUP_SKIPPED_TOTAL.labels(namespace=ns, reason="exact").inc()
            self._update_ratio(ns, action)
            decisions[i] = DedupDecision(action=action, content_hash=h)

        # Phase 3: соберём тексты для semantic dedup (только те, что не exact-match)
        to_embed_indices = [i for i, d in enumerate(decisions) if d is None]
        if not to_embed_indices:
            return decisions  # type: ignore[return-value]

        texts_to_embed = [entries[i]["content"] for i in to_embed_indices]

        # Batch embedding — один запрос вместо N
        embeddings = await self.embedding.embed_many(texts_to_embed)

        # Phase 4: semantic dedup — параллельные проверки (gather)
        async def _semantic_one(local_i: int, global_i: int) -> DedupDecision:
            entry = entries[global_i]
            ns = entry.get("namespace", "default")
            h = hashes[global_i]
            vector = embeddings[local_i]
            threshold = self.runtime.get("dedup_thresholds").get(ns, self.runtime.get("dedup_threshold"))
            results = await self.repository.search(
                query_embedding=vector,
                user_id=user_id,
                namespace=ns,
                threshold=threshold,
                limit=5,
            )
            best = self._semantic_match(results, threshold, entry.get("metadata"))
            if best is not None:
                DEDUP_SKIPPED_TOTAL.labels(namespace=ns, reason="semantic").inc()
                self._update_ratio(ns, DedupAction.SKIP)
                return DedupDecision(
                    action=DedupAction.SKIP,
                    existing_id=best.id,
                    existing_score=best.score,
                    content_hash=h,
                )
            DEDUP_INSERTED_TOTAL.labels(namespace=ns).inc()
            self._update_ratio(ns, DedupAction.INSERT)
            return DedupDecision(
                action=DedupAction.INSERT,
                content_hash=h,
                embedding=vector,
            )

        gathered = await asyncio.gather(
            *(_semantic_one(li, gi) for li, gi in enumerate(to_embed_indices)),
            return_exceptions=True,
        )
        for li, gi in enumerate(to_embed_indices):
            res = gathered[li]
            if isinstance(res, BaseException):
                # Потерять дубль безопаснее, чем данные: INSERT-fallback
                logger.warning(
                    "batch semantic dedup failed — INSERT fallback",
                    extra={"error": str(res), "error_type": type(res).__name__},
                )
                ns = entries[gi].get("namespace", "default")
                DEDUP_INSERTED_TOTAL.labels(namespace=ns).inc()
                self._update_ratio(ns, DedupAction.INSERT)
                res = DedupDecision(
                    action=DedupAction.INSERT,
                    content_hash=hashes[gi],
                    embedding=embeddings[li],
                )
            decisions[gi] = res

        return decisions  # type: ignore[return-value]
