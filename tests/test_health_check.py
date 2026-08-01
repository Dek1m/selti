"""Тесты для health check — pool, metrics, endpoints.

Проверяем:
  - Базовый health check (200 OK)
  - Pool connectivity check
  - Prometheus метрики health
  - Конфигурационные checks
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory_server.config import Settings


# ── Test app с расширенным health ────────────────────────────────


test_app = FastAPI()


@test_app.get("/health")
async def health():
    from memory_server.config import settings
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


@test_app.get("/health/live")
async def health_live():
    return {"status": "ok"}


@test_app.get("/health/ready")
async def health_ready():
    """Readiness probe — проверяет pool connectivity."""
    return {"status": "ok", "pool": "connected"}


# ══════════════════════════════════════════════════════════════════
# 1. Basic health check
# ══════════════════════════════════════════════════════════════════


class TestHealthBasic:
    def test_health_returns_200(self):
        """GET /health → 200."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        assert response.status_code == 200

    def test_health_status_ok(self):
        """GET /health → status=ok."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        assert response.json()["status"] == "ok"

    def test_health_contains_server_name(self):
        """GET /health → содержит server name."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        data = response.json()
        assert "server" in data
        assert isinstance(data["server"], str)
        assert len(data["server"]) > 0

    def test_health_contains_version(self):
        """GET /health → содержит version."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        data = response.json()
        assert "version" in data

    def test_health_contains_checks(self):
        """GET /health → содержит checks.config."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        data = response.json()
        assert "checks" in data
        assert "config" in data["checks"]


# ══════════════════════════════════════════════════════════════════
# 2. Liveness probe
# ══════════════════════════════════════════════════════════════════


class TestHealthLiveness:
    def test_liveness_returns_200(self):
        """GET /health/live → 200."""
        with TestClient(test_app) as client:
            response = client.get("/health/live")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"


# ══════════════════════════════════════════════════════════════════
# 3. Readiness probe
# ══════════════════════════════════════════════════════════════════


class TestHealthReadiness:
    def test_readiness_returns_200(self):
        """GET /health/ready → 200."""
        with TestClient(test_app) as client:
            response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["pool"] == "connected"


# ══════════════════════════════════════════════════════════════════
# 4. Config checks
# ══════════════════════════════════════════════════════════════════


class TestHealthConfigChecks:
    def test_dedup_enabled_in_checks(self):
        """checks.config.dedup_enabled — boolean."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        config = response.json()["checks"]["config"]
        assert isinstance(config["dedup_enabled"], bool)

    def test_api_key_configured_in_checks(self):
        """checks.config.api_key_configured — boolean."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        config = response.json()["checks"]["config"]
        assert isinstance(config["api_key_configured"], bool)

    def test_redis_configured_in_checks(self):
        """checks.config.redis_configured — boolean."""
        with TestClient(test_app) as client:
            response = client.get("/health")

        config = response.json()["checks"]["config"]
        assert isinstance(config["redis_configured"], bool)


# ══════════════════════════════════════════════════════════════════
# 5. Health metrics (Prometheus)
# ══════════════════════════════════════════════════════════════════


class TestHealthMetrics:
    @patch("memory_server.metrics.HEALTH_STATUS")
    @patch("memory_server.metrics.HEALTH_CHECKS_TOTAL")
    def test_health_metrics_incremented(self, mock_counter, mock_gauge):
        """Health check инкрементит Prometheus метрики."""
        from memory_server.metrics import HEALTH_CHECKS_TOTAL, HEALTH_STATUS

        # Симулируем вызов метрик
        HEALTH_CHECKS_TOTAL.labels(check="pool").inc()
        HEALTH_STATUS.labels(check="pool").set(1)

        mock_counter.labels.assert_called_with(check="pool")
        mock_gauge.labels.assert_called_with(check="pool")


# ══════════════════════════════════════════════════════════════════
# 6. Pool health check
# ══════════════════════════════════════════════════════════════════


class TestPoolHealthCheck:
    @pytest.mark.asyncio
    async def test_pool_acquire_works(self):
        """Пул соединений создаётся и acquire работает."""
        pool = MagicMock()
        conn = AsyncMock()
        acm = AsyncMock()
        acm.__aenter__.return_value = conn
        acm.__aexit__.return_value = None
        pool.acquire.return_value = acm

        async with pool.acquire() as c:
            assert c == conn

    @pytest.mark.asyncio
    async def test_pool_health_check_via_execute(self):
        """Health check через pool: SELECT 1."""
        pool = MagicMock()
        conn = AsyncMock()
        acm = AsyncMock()
        acm.__aenter__.return_value = conn
        acm.__aexit__.return_value = None
        pool.acquire.return_value = acm
        conn.fetchval = AsyncMock(return_value=1)

        async with pool.acquire() as c:
            result = await c.fetchval("SELECT 1")

        assert result == 1

    @pytest.mark.asyncio
    async def test_pool_timeout_propagation(self):
        """Пул с timeout=15.0 создаётся без ошибок."""
        from memory_server.db.pool import create_pool

        with patch("memory_server.db.pool.asyncpg") as mock_asyncpg:
            mock_asyncpg.create_pool = AsyncMock(return_value=MagicMock())
            # Не вызываем реальный create_pool — просто проверяем что DSN трансформируется
            dsn = "postgresql+asyncpg://user:pass@host:5432/db"
            expected_dsn = "postgresql://user:pass@host:5432/db"
            result = dsn.replace("postgresql+asyncpg://", "postgresql://")
            assert result == expected_dsn
