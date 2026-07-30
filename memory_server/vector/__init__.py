from qdrant_client import QdrantClient

from memory_server.config import Settings


async def create_qdrant_client(config: Settings) -> QdrantClient | None:
    """Factory: returns QdrantClient if enabled, None otherwise."""
    if not config.qdrant_enabled:
        return None
    client = QdrantClient(url=config.qdrant_url, timeout=30)
    return client
