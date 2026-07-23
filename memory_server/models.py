from datetime import datetime

from pydantic import BaseModel, Field


class MemoryRecord(BaseModel):
    id: str
    user_id: str
    content: str
    metadata: dict = Field(default_factory=dict)
    namespace: str = "default"
    created_at: datetime
    updated_at: datetime
    content_hash: str | None = None


class MemoryInput(BaseModel):
    content: str
    user_id: str
    metadata: dict = Field(default_factory=dict)
    namespace: str = "default"
    content_hash: str | None = None


class SearchResult(BaseModel):
    id: str
    content: str
    metadata: dict = Field(default_factory=dict)
    score: float


class MemoryListResult(BaseModel):
    items: list[MemoryRecord]
    total: int


class DeleteResult(BaseModel):
    success: bool = True


class ForgetResult(BaseModel):
    deleted_count: int
