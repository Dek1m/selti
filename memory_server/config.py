import os
from enum import Enum

from pydantic_settings import BaseSettings


class Namespace(str, Enum):
    DEFAULT = "default"
    USER_FACTS = "user_facts"
    CODE_KNOWLEDGE = "code_knowledge"
    DIALOGUE_INSIGHTS = "dialogue_insights"
    PROJECT_META = "project_meta"
    INFRASTRUCTURE = "infrastructure"


class Settings(BaseSettings):
    database_url: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://svc_athene_ai:changeme@localhost:5432/memory")
    db_min_connections: int = 2
    db_max_connections: int = 10

    embedding_api_url: str = "http://10.0.0.21:8080/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen3-embedding-8b"
    embedding_dimension: int = 4096

    # ── Qdrant: векторное хранилище ──
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "memories"
    qdrant_api_key: str = ""  # для Qdrant Cloud (опционально)
    qdrant_enabled: bool = True  # False = только PostgreSQL без векторного поиска

    mcp_server_name: str = os.getenv("SERVICE_NAME", "selti")
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    search_default_limit: int = 10
    search_default_threshold: float = 0.7

    dedup_enabled: bool = True
    dedup_threshold: float = 0.95
    dedup_thresholds: dict[str, float] = {
        "default": 0.95,
        "user_facts": 0.90,
        "dialogue_insights": 0.85,
        "code_knowledge": 0.95,
        "project_meta": 0.90,
        "infrastructure": 0.95,
    }

    api_key: str = ""

    redis_url: str = "redis://:@redis:6379/0"

    # ── Celery: асинхронные задачи ──
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"
    celery_task_serializer: str = "json"
    celery_result_serializer: str = "json"
    celery_accept_content: list[str] = ["json"]
    celery_timezone: str = "UTC"
    celery_worker_concurrency: int = 4
    celery_worker_prefetch_multiplier: int = 1
    celery_worker_max_tasks_per_child: int = 1000
    celery_worker_max_memory_per_child: int = 200000  # 200MB

    log_level: str = "INFO"

    uvicorn_workers: int = 1

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
