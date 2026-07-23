import hashlib
import logging
from dataclasses import dataclass
from enum import Enum

from memory_server.config import Settings
from memory_server.embedding.provider import EmbeddingProvider
from memory_server.memory.repository import MemoryRepository

logger = logging.getLogger(__name__)


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

    async def check(
        self,
        content: str,
        user_id: str,
        namespace: str = "default",
    ) -> DedupDecision:
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        if not self.config.dedup_enabled:
            return DedupDecision(action=DedupAction.INSERT, content_hash=content_hash)

        # Exact dedup
        existing = await self.repository.find_by_content_hash(namespace, content_hash)
        if existing is not None:
            action = DedupAction.UPDATE if namespace == "user_facts" else DedupAction.SKIP
            logger.info(
                "Exact dedup match: namespace=%s action=%s id=%s",
                namespace, action.value, existing.id,
            )
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
            best = results[0]
            logger.info(
                "Semantic dedup match: namespace=%s score=%.4f id=%s",
                namespace, best.score, best.id,
            )
            return DedupDecision(
                action=DedupAction.SKIP,
                existing_id=best.id,
                existing_score=best.score,
                content_hash=content_hash,
            )

        return DedupDecision(
            action=DedupAction.INSERT,
            content_hash=content_hash,
        )
