from enum import Enum

from pydantic_settings import BaseSettings


class Namespace(str, Enum):
    DEFAULT = "default"
    USER_FACTS = "user_facts"
    CODE_KNOWLEDGE = "code_knowledge"
    DIALOGUE_INSIGHTS = "dialogue_insights"
    PROJECT_META = "project_meta"


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://athena:athena@localhost:5432/athena_memory"
    db_min_connections: int = 2
    db_max_connections: int = 20

    embedding_api_url: str = "http://10.0.0.21:8080/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen3-embedding-8b"
    embedding_dimension: int = 8192

    mcp_server_name: str = "athena-memory"
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
    }

    api_key: str = ""

    redis_url: str = "redis://:@redis:6379/0"

    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
