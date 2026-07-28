from datetime import datetime

from pydantic import BaseModel, Field


class MemoryRecord(BaseModel):
    id: str
    user_id: str
    content: str
    metadata: dict = Field(default_factory=dict)
    namespace: str = "default"
    importance: int = 3
    created_at: datetime
    updated_at: datetime
    content_hash: str | None = None


class MemoryInput(BaseModel):
    content: str
    user_id: str
    metadata: dict = Field(default_factory=dict)
    namespace: str = "default"
    importance: int = 3
    content_hash: str | None = None


class SearchResult(BaseModel):
    id: str
    content: str
    metadata: dict = Field(default_factory=dict)
    importance: int = 3
    score: float


class MemoryListResult(BaseModel):
    items: list[MemoryRecord]
    total: int


class DeleteResult(BaseModel):
    success: bool = True


class ForgetResult(BaseModel):
    deleted_count: int


class MemoryStatsItem(BaseModel):
    namespace: str
    count: int
    last_updated: datetime | None = None


# ── Relation models ──

class Relation(BaseModel):
    """Связь между двумя гранулами (ребро графа)."""
    id: str
    source_id: str
    target_id: str | None = None
    target_name: str | None = None
    link_type: str
    description: str | None = None
    weight: float = 1.0
    metadata: dict = Field(default_factory=dict)
    created_at: datetime


class RelationCreate(BaseModel):
    """Данные для создания связи."""
    source_id: str
    target_id: str | None = None
    target_name: str | None = None
    link_type: str
    description: str | None = None
    weight: float = 1.0
    metadata: dict = Field(default_factory=dict)


class RelationListResult(BaseModel):
    """Результат: входящие и исходящие связи."""
    incoming: list[Relation]
    outgoing: list[Relation]


class TraverseResult(BaseModel):
    """Результат обхода графа."""
    nodes: list[dict]  # [{id, content, namespace, ...}]
    edges: list[Relation]


class GraphStats(BaseModel):
    """Статистика графа."""
    total_granules: int
    total_relations: int
    linked_granules: int
    orphans: int
    avg_connections: float
    by_namespace: dict[str, dict]  # {namespace: {linked, orphans}}
    by_link_type: dict[str, int]   # {link_type: count}
