from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ── Жизненный цикл гранулы (миграция 018, D3) ──
MemoryStatus = Literal["asserted", "superseded", "retracted", "uncertain"]

# ── Типы связей: CHECK из migrations/005_relations.sql (27) + расширение 018 (+5) ──
LinkType = Literal[
    # Кодовые
    "depends_on", "used_by",
    "extends", "implements",
    "contains", "contained_by",
    "calls", "called_by",
    # Общие
    "related_to", "contradicts", "solves", "tested_by",
    "implements_adr", "references",
    "follows", "precedes",
    "alternative_to", "causes", "prevents",
    # Инфраструктурные
    "runs_on", "exposes", "mounts",
    # Cross-namespace
    "derived_from", "motivates",
    "informs", "informed_by", "connected_to",
    # Миграция 018: supersession-цепочки и кластеры
    "supersedes", "supports", "member_of", "part_of", "describes_cluster",
]


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
    # ── Каноническая гранула (миграция 018). Новые поля — в конце JSON-контракта ──
    project_id: UUID | None = None
    status: MemoryStatus = "asserted"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    ingested_at: datetime | None = None
    confidence: float = 1.0
    supersedes: UUID | None = None
    superseded_by: UUID | None = None
    frozen: bool = False
    last_accessed_at: datetime | None = None
    access_count: int = 0


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
    # ── Расширение Фазы 0: контекст гранулы в выдаче (наполняется волной 2) ──
    project_id: UUID | None = None
    status: MemoryStatus = "asserted"


class MemoryListResult(BaseModel):
    items: list[MemoryRecord]
    total: int


class DeleteResult(BaseModel):
    success: bool = True


class ForgetResult(BaseModel):
    deleted_count: int


class MemoryHistory(BaseModel):
    """Supersession-цепочка гранулы (Фаза 2.1, D3).

    items — от старейшей к новейшей версии; current_id — актуальная
    версия (status='asserted'), None — если вся цепочка закрыта.
    """
    items: list[MemoryRecord]
    current_id: str | None = None


class ClusterRecord(BaseModel):
    """Кластер схожих гранул Level 2 (Фаза 2.3, миграция 022)."""
    id: str
    namespace: str
    label: str | None = None
    summary: str | None = None
    member_count: int = 0
    coherence: float | None = None
    last_computed_at: datetime | None = None


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
    created_at: datetime | None = None


class RelationCreate(BaseModel):
    """Данные для создания связи.

    link_type валидируется Literal'ом LinkType: недопустимый тип даёт
    pydantic ValidationError со списком разрешённых значений ещё на входе,
    а не PG CHECK violation в глубине воркера.
    """
    source_id: str
    target_id: str | None = None
    target_name: str | None = None
    link_type: LinkType = "related_to"
    description: str | None = None
    weight: float = 1.0
    metadata: dict = Field(default_factory=dict)


class RelationListResult(BaseModel):
    """Результат: входящие и исходящие связи."""
    incoming: list[Relation]
    outgoing: list[Relation]


class TraverseResult(BaseModel):
    """Результат обхода графа.

    total_nodes/truncated (Фаза 1.5): cap + пагинация в Python-слое —
    total_nodes для навигации, truncated=True — выдача урезана cap'ом.
    """
    nodes: list[dict]  # [{id, content, namespace, ...}]
    edges: list[Relation]
    total_nodes: int = 0
    truncated: bool = False


class GraphStats(BaseModel):
    """Статистика графа."""
    total_granules: int
    total_relations: int
    linked_granules: int
    orphans: int
    avg_connections: float
    by_namespace: dict[str, dict]  # {namespace: {linked, orphans}}
    by_link_type: dict[str, int]   # {link_type: count}


# ── Project context («облачко знаний», D9) ──

class ProjectContext(BaseModel):
    """Снапшот контекста проекта (миграция 019).

    sections: {stack, decisions, code, insights, infra, <прочие ns>} —
    стек из project_technologies/project_links + топ-гранулы по namespace
    (хранимка project_context_snapshot); проза Тиши (sections.prose) —
    поверх механической сборки (Фаза 6.2).
    stale: true — после снапшота были записи в проект (dirty-флаг),
    показываем как есть, но честно помечаем (Фаза 6.1).
    """
    project_id: str
    content: str | None = None
    sections: dict = Field(default_factory=dict)
    granule_count: int = 0
    computed_at: datetime | None = None
    stale: bool = False
