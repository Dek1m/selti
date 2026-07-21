from pydantic_settings import BaseSettings


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

    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
