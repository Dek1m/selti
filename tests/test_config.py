import os
from unittest.mock import patch

import pytest

from memory_server.config import Settings


class TestSettingsDefaults:
    """Verify default values match the model definition."""

    def test_defaults_without_env(self):
        """Should use defaults when no env vars are set."""
        with patch.dict(os.environ, {}, clear=True):
            s = Settings()  # type: ignore[call-arg]
        assert s.database_url == "postgresql+asyncpg://athena:athena@localhost:5432/athena_memory"
        assert s.db_min_connections == 2
        assert s.db_max_connections == 20
        assert s.embedding_api_url == "http://10.0.0.21:8080/v1"
        assert s.embedding_api_key == ""
        assert s.embedding_model == "qwen3-embedding-8b"
        assert s.embedding_dimension == 8192
        assert s.mcp_server_name == "athena-memory"
        assert s.search_default_limit == 10
        assert s.search_default_threshold == 0.7
        assert s.log_level == "INFO"


class TestSettingsFromEnv:
    """Verify that env vars override defaults."""

    ENV_OVERRIDES = {
        "DATABASE_URL": "postgresql+asyncpg://user:pass@host:9999/db",
        "DB_MIN_CONNECTIONS": "5",
        "DB_MAX_CONNECTIONS": "50",
        "EMBEDDING_API_URL": "http://custom:8081/v1",
        "EMBEDDING_API_KEY": "sk-my-key",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "EMBEDDING_DIMENSION": "1536",
        "MCP_SERVER_NAME": "my-memory",
        "SEARCH_DEFAULT_LIMIT": "25",
        "SEARCH_DEFAULT_THRESHOLD": "0.5",
        "LOG_LEVEL": "DEBUG",
    }

    def test_overrides(self):
        with patch.dict(os.environ, self.ENV_OVERRIDES, clear=True):
            s = Settings()  # type: ignore[call-arg]
        assert s.database_url == "postgresql+asyncpg://user:pass@host:9999/db"
        assert s.db_min_connections == 5
        assert s.db_max_connections == 50
        assert s.embedding_api_url == "http://custom:8081/v1"
        assert s.embedding_api_key == "sk-my-key"
        assert s.embedding_model == "text-embedding-3-small"
        assert s.embedding_dimension == 1536
        assert s.mcp_server_name == "my-memory"
        assert s.search_default_limit == 25
        assert s.search_default_threshold == 0.5
        assert s.log_level == "DEBUG"

    def test_partial_override(self):
        """Only override one field; others should fall back to defaults."""
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+asyncpg://custom:5432/mydb"}, clear=True):
            s = Settings()  # type: ignore[call-arg]
        assert s.database_url == "postgresql+asyncpg://custom:5432/mydb"
        # defaults unchanged
        assert s.embedding_api_url == "http://10.0.0.21:8080/v1"
        assert s.embedding_model == "qwen3-embedding-8b"


class TestSettingsBadValues:
    """Verify invalid env values are caught by Pydantic validation."""

    def test_invalid_dimension(self):
        with patch.dict(os.environ, {"EMBEDDING_DIMENSION": "not-a-number"}, clear=True):
            with pytest.raises(ValueError):
                Settings()  # type: ignore[call-arg]
