"""Backward-compatible re-export.

Все импорты `from memory_server.memory.repository_qdrant import MemoryRepository`
продолжают работать. Новый код → импортируй из memory_server.memory.repository.
"""
from memory_server.memory.repository import MemoryRepository

__all__ = ["MemoryRepository"]
