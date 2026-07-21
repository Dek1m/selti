from typing import Protocol


class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string."""
        ...

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple text strings in a batch."""
        ...
