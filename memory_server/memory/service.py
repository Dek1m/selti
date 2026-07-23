import logging

from memory_server.config import Settings
from memory_server.embedding.provider import EmbeddingProvider
from memory_server.exceptions import NotFoundError
from memory_server.memory.dedup import DedupAction, DedupEngine
from memory_server.memory.repository import MemoryRepository
from memory_server.models import MemoryListResult, MemoryRecord, SearchResult

logger = logging.getLogger(__name__)


class MemoryService:
    """Business logic layer for memory operations."""

    def __init__(
        self,
        repository: MemoryRepository,
        embedding_provider: EmbeddingProvider,
        config: Settings | None = None,
    ):
        self.repository = repository
        self.embedding = embedding_provider
        self.config = config or Settings()
        self.dedup = DedupEngine(repository, embedding_provider, self.config)

    async def store(
        self,
        content: str,
        user_id: str,
        metadata: dict | None = None,
        namespace: str | None = None,
    ) -> tuple[MemoryRecord, DedupAction]:
        namespace = namespace or "default"
        content_hash: str | None = None

        if self.config.dedup_enabled:
            decision = await self.dedup.check(content, user_id, namespace)
            content_hash = decision.content_hash

            if decision.action == DedupAction.SKIP:
                record = await self.repository.get_by_id(decision.existing_id)
                if record is None:
                    raise RuntimeError(f"Failed to retrieve existing memory: {decision.existing_id}")
                return record, DedupAction.SKIP

            if decision.action == DedupAction.UPDATE:
                record = await self.repository.get_by_id(decision.existing_id)
                if record is None:
                    raise RuntimeError(f"Failed to retrieve memory for update: {decision.existing_id}")
                updated = await self.repository.update(
                    memory_id=decision.existing_id,
                    metadata={**record.metadata, **(metadata or {})},
                )
                if updated is None:
                    raise RuntimeError(f"Failed to update memory: {decision.existing_id}")
                return updated, DedupAction.UPDATE

        embedding = await self.embedding.embed(content)
        memory_id = await self.repository.insert(
            user_id=user_id,
            content=content,
            embedding=embedding,
            metadata=metadata or {},
            namespace=namespace,
            content_hash=content_hash,
        )
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            raise RuntimeError(f"Failed to retrieve memory after insert: {memory_id}")
        return record, DedupAction.INSERT

    async def search(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        threshold: float = 0.7,
        namespace: str | None = None,
    ) -> list[SearchResult]:
        query_embedding = await self.embedding.embed(query)
        return await self.repository.search(
            query_embedding=query_embedding,
            user_id=user_id,
            limit=limit,
            threshold=threshold,
            namespace=namespace,
        )

    async def get(self, memory_id: str) -> MemoryRecord:
        record = await self.repository.get_by_id(memory_id)
        if record is None:
            raise NotFoundError(memory_id)
        return record

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict | None = None,
    ) -> MemoryRecord:
        embedding = None
        if content is not None:
            embedding = await self.embedding.embed(content)
        record = await self.repository.update(
            memory_id=memory_id,
            content=content,
            embedding=embedding,
            metadata=metadata,
        )
        if record is None:
            raise NotFoundError(memory_id)
        return record

    async def delete(self, memory_id: str) -> bool:
        return await self.repository.delete(memory_id)

    async def list(
        self,
        user_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryListResult:
        return await self.repository.list(
            user_id=user_id,
            namespace=namespace,
            limit=limit,
            offset=offset,
        )

    async def forget(
        self,
        user_id: str,
        namespace: str | None = None,
    ) -> int:
        return await self.repository.forget(
            user_id=user_id,
            namespace=namespace,
        )
