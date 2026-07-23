import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory_server.config import settings

# ---------------------------------------------------------------------------
# Тестовое FastAPI-приложение с health-ендпоинтом (без реального MCP сервера)
# ---------------------------------------------------------------------------

test_app = FastAPI()


@test_app.get("/health")
async def health():
    return {
        "status": "ok",
        "server": settings.mcp_server_name,
        "version": "0.1.0",
        "checks": {
            "config": {
                "dedup_enabled": settings.dedup_enabled,
                "api_key_configured": bool(settings.api_key),
                "redis_configured": bool(settings.redis_url),
            }
        },
    }


class TestHealth:
    def test_health_returns_200_with_status_ok(self):
        """GET /health → 200, содержит status=ok."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_health_contains_server_and_version(self):
        """GET /health → содержит server и version."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        data = response.json()
        assert "server" in data
        assert data["server"] == "athena-memory"
        assert "version" in data
        assert data["version"] == "0.1.0"

    def test_health_contains_checks_config(self):
        """GET /health → содержит checks.config."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        data = response.json()
        assert "checks" in data
        assert "config" in data["checks"]
        assert "dedup_enabled" in data["checks"]["config"]
        assert "api_key_configured" in data["checks"]["config"]
        assert "redis_configured" in data["checks"]["config"]
