from qdrant_client import QdrantClient

from memory_server.config import Settings


async def create_qdrant_client(config: Settings) -> QdrantClient | None:
    """Factory: returns QdrantClient if enabled, None otherwise."""
    if not config.qdrant_enabled:
        return None
    # Парсим URL для host/port чтобы избежать бага qdrant-client
    url = config.qdrant_url.replace("http://", "").replace("https://", "")
    host, port_str = url.split(":")
    port = int(port_str.rstrip("/"))
    client = QdrantClient(host=host, port=port, timeout=30)
    return client
