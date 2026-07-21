import logging

import httpx

from memory_server.embedding.provider import EmbeddingProvider
from memory_server.exceptions import EmbeddingError

logger = logging.getLogger(__name__)


class EmbeddingClient(EmbeddingProvider):
    """OpenAI-compatible embedding API client."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        dimension: int,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                headers=headers,
                timeout=httpx.Timeout(30.0),
            )
            await self._verify_dimension()
        return self._client

    async def _verify_dimension(self) -> None:
        """Verify the embedding dimension matches configuration."""
        test_embedding = await self._request_embedding("verify")
        actual = len(test_embedding)
        if actual != self.dimension:
            logger.warning(
                "Embedding dimension mismatch: configured=%d, actual=%d. Using actual=%d.",
                self.dimension, actual, actual,
            )
            self.dimension = actual

    async def _request_embedding(self, text: str) -> list[float]:
        client = await self._get_client()
        response = await client.post(
            "/embeddings",
            json={"model": self.model, "input": text},
        )
        if response.status_code != 200:
            detail = response.text
            try:
                body = response.json()
                detail = body.get("error", {}).get("message", detail)
            except Exception:
                pass
            raise EmbeddingError(status_code=response.status_code, detail=detail)
        data = response.json()
        return data["data"][0]["embedding"]

    async def embed(self, text: str) -> list[float]:
        return await self._request_embedding(text)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        client = await self._get_client()
        response = await client.post(
            "/embeddings",
            json={"model": self.model, "input": texts},
        )
        if response.status_code != 200:
            detail = response.text
            try:
                body = response.json()
                detail = body.get("error", {}).get("message", detail)
            except Exception:
                pass
            raise EmbeddingError(status_code=response.status_code, detail=detail)
        data = response.json()
        data["data"].sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data["data"]]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        await self._get_client()
        return self

    async def __aexit__(self, *args):
        await self.aclose()
