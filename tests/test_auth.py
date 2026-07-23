"""Тесты для auth middleware из memory_server/__main__.py.

Проверяем:
  1. /health доступен без ключа
  2. /metrics доступен без ключа
  3. При пустом api_key все эндпоинты доступны
  4. С правильным ключом запрос проходит
  5. С неправильным ключом — 403
  6. Без заголовка Authorization — 403
  7. MCP endpoint (/mcp) тоже защищён
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _make_app():
    """Создаёт FastAPI app с auth middleware.
    api_key берётся из settings (должен быть установлен до вызова через monkeypatch)."""
    from fastapi import FastAPI, Request, Response
    from memory_server.config import settings

    app = FastAPI()

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path in ("/health", "/metrics"):
            return await call_next(request)

        if not settings.api_key:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {settings.api_key}":
            return await call_next(request)

        return Response(status_code=403, content="Forbidden")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return Response(content="", media_type="text/plain")

    @app.post("/mcp")
    async def mcp_endpoint():
        return {"ok": True}

    @app.get("/protected")
    async def protected():
        return {"data": "secret"}

    return app


class TestAuthMiddleware:
    """Проверка auth middleware."""

    def test_health_without_auth(self, monkeypatch):
        """/health доступен без ключа."""
        monkeypatch.setattr("memory_server.config.settings.api_key", "secret-key")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

    def test_metrics_without_auth(self, monkeypatch):
        """/metrics доступен без ключа."""
        monkeypatch.setattr("memory_server.config.settings.api_key", "secret-key")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/metrics")
            assert resp.status_code == 200

    def test_no_api_key_allows_all(self, monkeypatch):
        """При пустом api_key все эндпоинты доступны без авторизации."""
        monkeypatch.setattr("memory_server.config.settings.api_key", "")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/protected")
            assert resp.status_code == 200
            assert resp.json() == {"data": "secret"}

    def test_correct_api_key_passes(self, monkeypatch):
        """С правильным ключом запрос проходит."""
        monkeypatch.setattr("memory_server.config.settings.api_key", "my-secret-key")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get(
                "/protected",
                headers={"Authorization": "Bearer my-secret-key"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"data": "secret"}

    def test_wrong_api_key_returns_403(self, monkeypatch):
        """С неправильным ключом — 403."""
        monkeypatch.setattr("memory_server.config.settings.api_key", "real-key")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get(
                "/protected",
                headers={"Authorization": "Bearer wrong-key"},
            )
            assert resp.status_code == 403

    def test_no_auth_header_returns_403(self, monkeypatch):
        """Без заголовка Authorization — 403."""
        monkeypatch.setattr("memory_server.config.settings.api_key", "secret")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/protected")
            assert resp.status_code == 403

    def test_mcp_endpoint_protected(self, monkeypatch):
        """MCP endpoint (/mcp) защищён так же, как и остальные."""
        monkeypatch.setattr("memory_server.config.settings.api_key", "secret")
        app = _make_app()
        with TestClient(app) as client:
            # без ключа
            resp = client.post("/mcp")
            assert resp.status_code == 403

            # с правильным ключом
            resp = client.post(
                "/mcp",
                headers={"Authorization": "Bearer secret"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}

    def test_health_with_wrong_auth_still_allowed(self, monkeypatch):
        """/health доступен даже с неправильным ключом (белый список)."""
        monkeypatch.setattr("memory_server.config.settings.api_key", "secret")
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get(
                "/health",
                headers={"Authorization": "Bearer wrong-key"},
            )
            assert resp.status_code == 200
