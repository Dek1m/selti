import hashlib
import logging
from dataclasses import dataclass
from enum import Enum

from memory_server.config import Settings
from memory_server.embedding.provider import EmbeddingProvider
from memory_server.memory.repository_qdrant import MemoryRepository
from memory_server.metrics import DEDUP_SKIPPED_TOTAL, DEDUP_INSERTED_TOTAL, DEDUP_RATIO

logger = logging.getLogger(__name__)

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
        config: Settings,
    ):
        self.repository = repository
        self.embedding = embedding_client
        self.config = config

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

    async def check(
        self,
        content: str,
        user_id: str,
        namespace: str = "default",
        metadata: dict | None = None,
    ) -> DedupDecision:
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        if not self.config.dedup_enabled:
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
        threshold = self.config.dedup_thresholds.get(namespace, self.config.dedup_threshold)
        vector = await self.embedding.embed(content)
        results = await self.repository.search(
            query_embedding=vector,
            user_id=user_id,
            namespace=namespace,
            threshold=threshold,
            limit=5,
        )

        if results and results[0].score >= threshold:
            # Проверка entity_name: если разный — не дубль
            incoming_entity = (metadata or {}).get("entity_name", "").strip().lower()
            existing_entity = (results[0].metadata or {}).get("entity_name", "").strip().lower()

            if incoming_entity and existing_entity and incoming_entity != existing_entity:
                logger.info("Semantic dedup skipped (different entity_name)", extra={
                    "incoming": incoming_entity, "existing": existing_entity,
                })
                # Не дубль — entity_name разный, гранулы дополняют друг друга
            else:
                best = results[0]
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
        """Batch dedup: exact hash check для всех, затем batch embedding + semantic search.

        Оптимизация: вместо serial embed() × N делаем batch embed_many() один раз.
        """
        if not self.config.dedup_enabled:
            return [
                DedupDecision(
                    action=DedupAction.INSERT,
                    content_hash=hashlib.sha256(e["content"].encode()).hexdigest(),
                )
                for e in entries
            ]

        # Phase 1: content hashes для всех entries (CPU-only, мгновенно)
        hashes = [hashlib.sha256(e["content"].encode()).hexdigest() for e in entries]

        # Phase 2: exact dedup — batch lookup по namespace + hash
        # Группируем по namespace для эффективных запросов
        ns_groups: dict[str, list[tuple[int, str]]] = {}
        for i, (entry, h) in enumerate(zip(entries, hashes)):
            ns = entry.get("namespace", "default")
            ns_groups.setdefault(ns, []).append((i, h))

        decisions: list[DedupDecision | None] = [None] * len(entries)
        for ns, items in ns_groups.items():
            for idx, h in items:
                existing = await self.repository.find_by_content_hash(ns, h)
                if existing is not None:
                    action = DedupAction.UPDATE if ns == "user_facts" else DedupAction.SKIP
                    DEDUP_SKIPPED_TOTAL.labels(namespace=ns, reason="exact").inc()
                    self._update_ratio(ns, action)
                    decisions[idx] = DedupDecision(
                        action=action,
                        existing_id=existing.id,
                        content_hash=h,
                    )

        # Phase 3: соберём тексты для semantic dedup (только те, что не exact-match)
        to_embed_indices = [i for i, d in enumerate(decisions) if d is None]
        if not to_embed_indices:
            return decisions  # type: ignore[return-value]

        texts_to_embed = [entries[i]["content"] for i in to_embed_indices]

        # Batch embedding — один запрос вместо N
        embeddings = await self.embedding.embed_many(texts_to_embed)

        # Phase 4: semantic dedup для каждого кандидата
        for local_i, global_i in enumerate(to_embed_indices):
            entry = entries[global_i]
            ns = entry.get("namespace", "default")
            h = hashes[global_i]
            vector = embeddings[local_i]

            threshold = self.config.dedup_thresholds.get(ns, self.config.dedup_threshold)
            results = await self.repository.search(
                query_embedding=vector,
                user_id=user_id,
                namespace=ns,
                threshold=threshold,
                limit=5,
            )

            if results and results[0].score >= threshold:
                incoming_entity = (entry.get("metadata") or {}).get("entity_name", "").strip().lower()
                existing_entity = (results[0].metadata or {}).get("entity_name", "").strip().lower()

                if incoming_entity and existing_entity and incoming_entity != existing_entity:
                    pass  # не дубль — entity_name разный
                else:
                    best = results[0]
                    DEDUP_SKIPPED_TOTAL.labels(namespace=ns, reason="semantic").inc()
                    self._update_ratio(ns, DedupAction.SKIP)
                    decisions[global_i] = DedupDecision(
                        action=DedupAction.SKIP,
                        existing_id=best.id,
                        existing_score=best.score,
                        content_hash=h,
                    )
                    continue

            DEDUP_INSERTED_TOTAL.labels(namespace=ns).inc()
            self._update_ratio(ns, DedupAction.INSERT)
            decisions[global_i] = DedupDecision(
                action=DedupAction.INSERT,
                content_hash=h,
                embedding=vector,
            )

        return decisions  # type: ignore[return-value]
