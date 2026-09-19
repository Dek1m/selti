import os
from pathlib import Path

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
    from memory_server.__main__ import _server_version
    return {
        "status": "ok",
        "server": settings.mcp_server_name,
        "version": _server_version(),
        "checks": {
            "config": {
                "dedup_enabled": settings.dedup_enabled,
                "api_key_configured": bool(settings.api_key),
                "redis_configured": bool(settings.redis_url),
            }
        },
    }


# Liveness: реальный обработчик из __main__ — лёгкий 200-ok без
# проверок зависимостей (Фаза 3.3; readiness остаётся на /health)
from memory_server.__main__ import live as _live_handler


@test_app.get("/live")
async def live():
    return await _live_handler()


class TestServerVersion:
    def test_server_version_reads_version_file(self):
        """_server_version — единственный источник правды: VERSION-файл корня репо."""
        from memory_server.__main__ import _server_version

        root_version = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
        assert root_version, "VERSION-файл пуст"
        assert _server_version() == root_version

    def test_server_version_is_semver(self):
        """Версия из VERSION-файла — semver-строка (не legacy-константа 0.1.0)."""
        from memory_server.__main__ import _server_version

        version = _server_version()
        assert version != "0.1.0"
        assert version != "unknown"
        parts = version.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts), (
            f"ожидался semver вида X.Y.Z, получен {version!r}"
        )


class TestHealth:
    def test_health_returns_200_with_status_ok(self):
        """GET /health → 200, содержит status=ok."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestLive:
    def test_live_returns_200_without_dependency_checks(self):
        """GET /live → 200 мгновенно: liveness не зависит от бэкендов."""
        with TestClient(test_app) as client:
            response = client.get("/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"
        assert data["server"] == os.getenv("SERVICE_NAME", "selti")
        # Liveness — лёгкий: никаких checks зависимостей в ответе
        assert "checks" not in data

    def test_health_contains_server_and_version(self):
        """GET /health → содержит server и version (из VERSION-файла)."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        data = response.json()
        assert "server" in data
        assert data["server"] == os.getenv("SERVICE_NAME", "selti")
        assert "version" in data
        root_version = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
        assert data["version"] == root_version

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
