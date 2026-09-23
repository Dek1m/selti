"""REST-тесты /api/settings (Ф2): мок state (репозиторий + runtime),
мини-FastAPI, полный CRUD + профили + коды ответов."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import memory_server.api.settings as api_settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.settings_store import (
    REGISTRY,
    SettingsRepository,
)
from tests.test_settings_store import FakePool  # мок-инфраструктура


class FakeState:
    def __init__(self, repo: SettingsRepository, runtime: RuntimeConfig) -> None:
        self._repo = repo
        self._runtime = runtime

    async def get_settings_repository(self) -> SettingsRepository:
        return self._repo

    async def get_runtime_config(self) -> RuntimeConfig:
        return self._runtime


@pytest.fixture
def client(monkeypatch):
    pool = FakePool()
    repo = SettingsRepository(pool)
    runtime = RuntimeConfig()
    runtime.bind(repo)
    monkeypatch.setattr(
        api_settings, "get_state", lambda: FakeState(repo, runtime)
    )
    monkeypatch.setattr(api_settings, "_broadcast_concurrency", lambda old, new: None)
    app = FastAPI()
    app.include_router(api_settings.router)
    return TestClient(app), pool, runtime


class TestListAndGet:
    def test_list_contains_all_97_with_sources(self, client):
        http, _, _ = client
        response = http.get("/api/settings")
        assert response.status_code == 200
        body = response.json()
        assert len(body["settings"]) == 97
        assert {g["key"] for g in body["groups"]} == {
            s.group for s in REGISTRY.values()
        }
        item = next(s for s in body["settings"] if s["key"] == "stale_days")
        assert item["effective_source"] == "default"
        assert item["differs_from_default"] is False
        assert item["default_value"] == 30
        assert item["requires_restart"] is False

    def test_get_single(self, client):
        http, _, _ = client
        assert http.get("/api/settings/rrf_k").json()["key"] == "rrf_k"
        assert http.get("/api/settings/nope").status_code == 404

    def test_secrets_never_present(self, client):
        http, _, _ = client
        keys = {s["key"] for s in http.get("/api/settings").json()["settings"]}
        assert "api_key" not in keys
        assert "linker_llm_api_key" not in keys


class TestPut:
    def test_valid_update(self, client):
        http, pool, runtime = client
        response = http.put("/api/settings/stale_days", json={"value": 45})
        assert response.status_code == 200
        assert response.json()["value"] == 45
        assert response.json()["effective_source"] == "db"
        # снапшот web-процесса обновился сразу (без NOTIFY)
        assert runtime.get("stale_days") == 45

    def test_validation_400(self, client):
        http, pool, _ = client
        response = http.put("/api/settings/rrf_k", json={"value": 99999})
        assert response.status_code == 400
        assert pool.store == {}

    def test_dangerous_without_confirm_409(self, client):
        http, _, _ = client
        response = http.put("/api/settings/gc_mode", json={"value": "hard"})
        assert response.status_code == 409
        assert response.json()["detail"]["keys"] == ["gc_mode"]

    def test_dangerous_with_confirm_200(self, client):
        http, _, _ = client
        response = http.put("/api/settings/gc_mode", json={"value": "hard", "confirm": True})
        assert response.status_code == 200
        assert response.json()["value"] == "hard"

    def test_unknown_key_404(self, client):
        http, _, _ = client
        assert http.put("/api/settings/nope", json={"value": 1}).status_code == 404

    def test_invariant_violation_400(self, client):
        http, _, _ = client
        response = http.put(
            "/api/settings/linker_synonym_threshold", json={"value": 0.9}
        )
        assert response.status_code == 400
        assert "invariant" in response.json()["detail"]["message"]


class TestReset:
    def test_reset_returns_to_default(self, client):
        http, pool, runtime = client
        http.put("/api/settings/stale_days", json={"value": 45})
        response = http.post("/api/settings/stale_days/reset", json={})
        assert response.status_code == 200
        assert response.json()["effective_source"] == "default"
        assert response.json()["db_value"] is None
        assert runtime.get("stale_days") == 30

    def test_reset_then_put_returns_full_ru_metadata(self, client):
        """Компромисс Ф4 (вариант «б»): reset→PUT пересоздаёт строку с
        ПОЛНЫМИ метаданными — GET отдаёт сидированные подписи, не пустоту."""
        http, _, _ = client
        http.put("/api/settings/stale_days", json={"value": 45})
        assert http.post("/api/settings/stale_days/reset", json={}).status_code == 200
        response = http.put("/api/settings/stale_days", json={"value": 50})
        assert response.status_code == 200
        body = response.json()
        assert body["title_ru"] == "Дней без доступа"
        assert body["description_ru"].startswith("Сколько дней гранула")
        # и GET одиночного ключа — тоже
        fetched = http.get("/api/settings/stale_days").json()
        assert fetched["title_ru"] == "Дней без доступа"
        assert fetched["description_ru"]

    def test_get_after_reset_keeps_ru_texts(self, client):
        """Строки нет вовсе (source=default): подписи всё равно непустые —
        fallback на SettingSpec, UI не теряет подписи."""
        http, _, _ = client
        body = http.get("/api/settings/rrf_k").json()
        assert body["title_ru"] == "Коэффициент RRF"
        assert body["description_ru"]

    def test_reset_dangerous_requires_confirm(self, client):
        http, _, _ = client
        http.put("/api/settings/gc_mode", json={"value": "hard", "confirm": True})
        assert http.post("/api/settings/gc_mode/reset", json={}).status_code == 409
        assert http.post("/api/settings/gc_mode/reset", json={"confirm": True}).status_code == 200

    def test_reset_all(self, client):
        http, _, _ = client
        http.put("/api/settings/stale_days", json={"value": 45})
        http.put("/api/settings/rrf_k", json={"value": 90})
        response = http.post("/api/settings/reset-all", json={"confirm": True})
        assert response.status_code == 200
        assert set(response.json()["reset"]) == {"stale_days", "rrf_k"}

    def test_reset_all_dangerous_409_nothing_reset(self, client):
        http, pool, _ = client
        http.put("/api/settings/stale_days", json={"value": 45})
        http.put("/api/settings/gc_mode", json={"value": "soft", "confirm": True})
        response = http.post("/api/settings/reset-all", json={})
        assert response.status_code == 409
        assert response.json()["detail"]["keys"] == ["gc_mode"]
        assert len(pool.store) == 2


class TestProfiles:
    def test_crud_flow(self, client):
        http, _, runtime = client
        http.put("/api/settings/stale_days", json={"value": 45})
        created = http.post(
            "/api/settings/profiles", json={"name": "tuned", "description": "снапшот"}
        )
        assert created.status_code == 201
        profile = created.json()
        # снапшот = текущие effective всех ключей
        assert profile["values"]["stale_days"] == 45

        listed = http.get("/api/settings/profiles").json()["profiles"]
        assert [p["name"] for p in listed] == ["tuned"]

        http.put("/api/settings/stale_days", json={"value": 50})
        refreshed = http.put("/api/settings/profiles/1", json={})
        assert refreshed.status_code == 200
        assert refreshed.json()["values"]["stale_days"] == 50

        assert http.delete("/api/settings/profiles/1").status_code == 200
        assert http.get("/api/settings/profiles").json()["profiles"] == []

    def test_builtin_delete_409(self, client):
        http, pool, _ = client
        pool.profiles[1] = {
            "id": 1, "name": "factory", "description": None, "is_builtin": True,
            "created_at": None, "applied_at": None, "values": {},
        }
        assert http.delete("/api/settings/profiles/1").status_code == 409

    def test_apply_profile(self, client):
        http, _, runtime = client
        created = http.post("/api/settings/profiles", json={"name": "p"}).json()
        profile_id = created["id"]
        # профиль — полный снапшот → содержит dangerous-ключи → confirm обязателен
        without_confirm = http.post(f"/api/settings/profiles/{profile_id}/apply", json={})
        assert without_confirm.status_code == 409
        response = http.post(
            f"/api/settings/profiles/{profile_id}/apply", json={"confirm": True}
        )
        assert response.status_code == 200
        assert "applied" in response.json()

    def test_apply_missing_404(self, client):
        http, _, _ = client
        assert http.post("/api/settings/profiles/999/apply", json={}).status_code == 404

    def test_profiles_route_not_shadowed_by_key(self, client):
        """GET /api/settings/profiles не должен матчиться как /{key}='profiles'."""
        http, _, _ = client
        assert http.get("/api/settings/profiles").status_code == 200
