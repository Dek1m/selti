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


class MemoryInput(BaseModel):
    content: str
    user_id: str
    metadata: dict = Field(default_factory=dict)
    namespace: str = "default"


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
