import hashlib
import json
import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class EmbeddingCache:
    """Redis-кеш для эмбеддингов. Ключ: sha256(text), значение: JSON-массив float."""

    def __init__(self, redis_url: str, ttl: int = 86400):
        self.redis_url = redis_url
        self.ttl = ttl
        self._client: Optional[aioredis.Redis] = None

    async def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(
                self.redis_url,
                decode_responses=True,
            )
        return self._client

    def _make_key(self, text: str) -> str:
        """Генерирует ключ: embedding:sha256hex."""
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        return f"embedding:{text_hash}"

    async def get(self, text: str) -> Optional[list[float]]:
        """Получить эмбеддинг из кеша. Miss → None."""
        client = await self._get_client()
        key = self._make_key(text)
        cached = await client.get(key)
        if cached is None:
            return None
        return json.loads(cached)

    async def set(self, text: str, embedding: list[float]) -> None:
        """Сохранить эмбеддинг в кеш."""
        client = await self._get_client()
        key = self._make_key(text)
        await client.setex(key, self.ttl, json.dumps(embedding))

    async def mget(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Batch-получение. Возвращает список (embedding или None)."""
        client = await self._get_client()
        keys = [self._make_key(t) for t in texts]
        cached = await client.mget(keys)
        result = []
        for val in cached:
            if val is None:
                result.append(None)
            else:
                result.append(json.loads(val))
        return result

    async def mset(self, pairs: list[tuple[str, list[float]]]) -> None:
        """Batch-сохранение: [(text, embedding), ...]."""
        client = await self._get_client()
        async with client.pipeline() as pipe:
            for text, embedding in pairs:
                key = self._make_key(text)
                await pipe.setex(key, self.ttl, json.dumps(embedding))
            await pipe.execute()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
