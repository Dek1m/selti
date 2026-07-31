import hashlib
import json
import logging
import time
from typing import Optional

import redis.asyncio as aioredis

from memory_server.metrics import (
    REDIS_OPS_TOTAL,
    REDIS_OPS_DURATION_SECONDS,
)

logger = logging.getLogger(__name__)

# Таймаут на Redis операции — 10 секунд
REDIS_TIMEOUT = 10.0


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
                socket_timeout=REDIS_TIMEOUT,
                socket_connect_timeout=REDIS_TIMEOUT,
            )
            logger.info("EmbeddingCache connected", extra={"url": self.redis_url})
        return self._client

    def _make_key(self, text: str) -> str:
        """Генерирует ключ: embedding:sha256hex."""
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        return f"embedding:{text_hash}"

    async def get(self, text: str) -> Optional[list[float]]:
        """Получить эмбеддинг из кеша. Miss → None."""
        client = await self._get_client()
        key = self._make_key(text)
        start = time.monotonic()
        try:
            cached = await client.get(key)
            duration = time.monotonic() - start
            REDIS_OPS_TOTAL.labels(operation="get").inc()
            REDIS_OPS_DURATION_SECONDS.labels(operation="get").observe(duration)
            if cached is None:
                return None
            return json.loads(cached)
        except Exception:
            REDIS_OPS_TOTAL.labels(operation="get").inc()
            raise

    async def set(self, text: str, embedding: list[float]) -> None:
        """Сохранить эмбеддинг в кеш."""
        client = await self._get_client()
        key = self._make_key(text)
        start = time.monotonic()
        try:
            await client.setex(key, self.ttl, json.dumps(embedding))
            duration = time.monotonic() - start
            REDIS_OPS_TOTAL.labels(operation="set").inc()
            REDIS_OPS_DURATION_SECONDS.labels(operation="set").observe(duration)
        except Exception:
            REDIS_OPS_TOTAL.labels(operation="set").inc()
            raise

    async def mget(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Batch-получение. Возвращает список (embedding или None)."""
        client = await self._get_client()
        keys = [self._make_key(t) for t in texts]
        start = time.monotonic()
        try:
            cached = await client.mget(keys)
            duration = time.monotonic() - start
            REDIS_OPS_TOTAL.labels(operation="mget").inc()
            REDIS_OPS_DURATION_SECONDS.labels(operation="mget").observe(duration)
            result = []
            for val in cached:
                if val is None:
                    result.append(None)
                else:
                    result.append(json.loads(val))
            return result
        except Exception:
            REDIS_OPS_TOTAL.labels(operation="mget").inc()
            raise

    async def mset(self, pairs: list[tuple[str, list[float]]]) -> None:
        """Batch-сохранение: [(text, embedding), ...]."""
        client = await self._get_client()
        start = time.monotonic()
        try:
            async with client.pipeline() as pipe:
                for text, embedding in pairs:
                    key = self._make_key(text)
                    await pipe.setex(key, self.ttl, json.dumps(embedding))
                await pipe.execute()
            duration = time.monotonic() - start
            REDIS_OPS_TOTAL.labels(operation="mset").inc()
            REDIS_OPS_DURATION_SECONDS.labels(operation="mset").observe(duration)
        except Exception:
            REDIS_OPS_TOTAL.labels(operation="mset").inc()
            raise

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
