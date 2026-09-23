"""Приёмочные сценарии Ф4 (Катерина, 2026-09-23) — независимая приёмка
системы конфигурации поверх тестов Соны.

Дыры, закрываемые здесь (выявлены аудитом покрытия test_settings_*):
- env-блокировка на УРОВНЕ API (у Соны только store-уровень);
- границы min/max ровно на min/max (у Соны только за-границей);
- типы: int-ключу bool, float-ключу str (bool-ключу int есть);
- инвариант линкера ЯВНО на границе равенства verdict == dedup[ns]
  (0.85 == 0.85 — дефолт сам живёт на этой границе, тест закрепляет);
- недоступная БД при старте: state.get_runtime_config не роняет процесс;
- reset-all с мусорной строкой вне реестра (баг: KeyError → 500, фикс).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import memory_server.api.settings as api_settings
from memory_server.runtime_config import RuntimeConfig
from memory_server.settings_store import (
    REGISTRY,
    SettingsRepository,
    SettingsValidationError,
    validate_linker_invariant,
    validate_value,
)
from tests.test_settings_store import FakePool


# ════════════════════════ API env-блокировка ════════════════════════


@pytest.fixture
def client_env_locked(monkeypatch):
    """API-клиент, где stale_days заблокирован env (как будто STALE_DAYS=77)."""
    pool = FakePool()
    repo = SettingsRepository(pool, env_locked_keys={"stale_days"})
    runtime = RuntimeConfig()
    # env-слой процесса: ключ задан в env (модель model_fields_set)
    runtime._env = {"stale_days": 77}
    runtime._rebuild()
    runtime.bind(repo)

    class FakeState:
        async def get_settings_repository(self):
            return repo

        async def get_runtime_config(self):
            return runtime

    monkeypatch.setattr(api_settings, "get_state", lambda: FakeState())
    monkeypatch.setattr(api_settings, "_broadcast_concurrency", lambda old, new: None)
    app = FastAPI()
    app.include_router(api_settings.router)
    return TestClient(app), pool, runtime


class TestEnvLockApi:
    def test_get_shows_env_lock(self, client_env_locked):
        http, _, _ = client_env_locked
        body = http.get("/api/settings/stale_days").json()
        assert body["effective_source"] == "env"
        assert body["is_env_locked"] is True
        assert body["value"] == 77

    def test_put_env_locked_409(self, client_env_locked):
        http, pool, _ = client_env_locked
        response = http.put("/api/settings/stale_days", json={"value": 45})
        assert response.status_code == 409
        assert pool.store == {}

    def test_reset_env_locked_409(self, client_env_locked):
        http, pool, _ = client_env_locked
        # строка могла остаться в БД с времён ДО включения env-блокировки
        pool.store["stale_days"] = {"value": 45}
        response = http.post("/api/settings/stale_days/reset", json={"confirm": True})
        assert response.status_code == 409
        assert pool.store != {}

    def test_unlocked_key_still_writable(self, client_env_locked):
        http, _, runtime = client_env_locked
        response = http.put("/api/settings/rrf_k", json={"value": 30})
        assert response.status_code == 200
        assert runtime.get("rrf_k") == 30
        # и env-ключ по-прежнему доминирует
        assert runtime.get("stale_days") == 77

    def test_apply_env_locked_goes_to_skipped(self, client_env_locked):
        http, pool, _ = client_env_locked
        pool.profiles[1] = {
            "id": 1, "name": "p", "description": None, "is_builtin": False,
            "created_at": None, "applied_at": None,
            "values": {"stale_days": 7, "rrf_k": 30},
        }
        response = http.post("/api/settings/profiles/1/apply", json={"confirm": True})
        assert response.status_code == 200
        report = response.json()
        assert report["skipped_env"] == ["stale_days"]
        assert report["applied"] == ["rrf_k"]


# ════════════════════════ Границы валидации ════════════════════════


class TestExactBoundaries:
    """Ровно min / ровно max — валидны; на шаг за — 400."""

    @pytest.mark.parametrize("value", [10, 1000])
    def test_int_bounds_inclusive(self, value):
        assert validate_value("hybrid_prefetch", value) == value

    @pytest.mark.parametrize("value", [9, 1001])
    def test_int_bounds_violated(self, value):
        with pytest.raises(SettingsValidationError):
            validate_value("hybrid_prefetch", value)

    @pytest.mark.parametrize("value", [0.0, 1.0])
    def test_float_bounds_inclusive(self, value):
        assert validate_value("search_default_threshold", value) == value

    @pytest.mark.parametrize("value", [-0.01, 1.01])
    def test_float_bounds_violated(self, value):
        with pytest.raises(SettingsValidationError):
            validate_value("search_default_threshold", value)

    @pytest.mark.parametrize("seconds", [10, 604800])
    def test_schedule_seconds_bounds_inclusive(self, seconds):
        ok = {"type": "interval", "seconds": seconds}
        assert validate_value("schedule.linker_l2_verdicts", ok) == ok

    @pytest.mark.parametrize("seconds", [9, 604801])
    def test_schedule_seconds_bounds_violated(self, seconds):
        with pytest.raises(SettingsValidationError):
            validate_value("schedule.linker_l2_verdicts", {"type": "interval", "seconds": seconds})


class TestTypeConfusion:
    def test_int_key_rejects_bool(self):
        with pytest.raises(SettingsValidationError):
            validate_value("rrf_k", True)

    def test_int_key_rejects_str(self):
        with pytest.raises(SettingsValidationError):
            validate_value("rrf_k", "60")

    def test_float_key_rejects_str(self):
        with pytest.raises(SettingsValidationError):
            validate_value("mmr_lambda", "0.7")

    def test_bool_key_rejects_int(self):
        with pytest.raises(SettingsValidationError):
            validate_value("hybrid_search_enabled", 1)

    def test_bool_key_rejects_str(self):
        with pytest.raises(SettingsValidationError):
            validate_value("hybrid_search_enabled", "yes")

    def test_str_key_rejects_int(self):
        with pytest.raises(SettingsValidationError):
            validate_value("linker_llm_model", 123)


# ════════════════════════ Инвариант линкера на границе ════════════════════════


class TestLinkerInvariantBoundary:
    """§2.5: synonym < verdict <= dedup_thresholds[ns].

    Дефолт живёт ровно на границе: verdict 0.85 == dialogue_insights 0.85.
    Равенство verdict == dedup[ns] ДОПУСТИМО (L2-зона ns вырождается),
    строгое превышение — отказ. synonym == verdict — отказ.
    """

    def test_equality_verdict_equals_dedup_ns_passes(self):
        # ровно граница: verdict 0.85 == dialogue_insights 0.85
        validate_linker_invariant({"linker_verdict_threshold": 0.85})
        validate_linker_invariant({"linker_synonym_threshold": 0.80, "linker_verdict_threshold": 0.85})

    def test_strictly_below_passes(self):
        validate_linker_invariant({"linker_verdict_threshold": 0.8499})

    def test_strictly_above_fails(self):
        with pytest.raises(SettingsValidationError):
            validate_linker_invariant({"linker_verdict_threshold": 0.850001})

    def test_synonym_equals_verdict_fails(self):
        with pytest.raises(SettingsValidationError):
            validate_linker_invariant({"linker_synonym_threshold": 0.85})

    def test_lowering_dedup_below_verdict_fails(self):
        # PUT dedup_thresholds с dialogue_insights 0.84 при verdict 0.85
        with pytest.raises(SettingsValidationError):
            validate_linker_invariant(
                {"dedup_thresholds": {"default": 0.95, "dialogue_insights": 0.84}}
            )

    @pytest.mark.asyncio
    async def test_boundary_through_repository_set(self):
        repo, _ = _make_repo()
        # ровно на границе — пишется
        await repo.set("linker_verdict_threshold", 0.85, effective={})
        # synonym 0.84 < verdict 0.85 == dedup 0.85 — пишется
        await repo.set("linker_synonym_threshold", 0.84, effective={"linker_verdict_threshold": 0.85})
        # synonym подтянут к verdict — отказ, ничего не записано
        with pytest.raises(SettingsValidationError):
            await repo.set("linker_synonym_threshold", 0.85, effective={"linker_verdict_threshold": 0.85})


def _make_repo(env_locked: set[str] | None = None):
    pool = FakePool()
    return SettingsRepository(pool, env_locked_keys=env_locked or set()), pool


# ════════════════════════ Reset-all и мусорные строки ════════════════════════


class TestResetAllWithStaleRows:
    @pytest.mark.asyncio
    async def test_unknown_db_row_does_not_crash_reset_all(self):
        """Ключ удалён из реестра, строка осталась в БД (реестр §8 меняется):
        reset-all НЕ должен падать KeyError → HTTP 500."""
        repo, pool = _make_repo()
        pool.store["legacy_key"] = {"value": 1}
        pool.store["stale_days"] = {"value": 60}
        reset = await repo.reset_all(confirm=True)
        assert set(reset) == {"legacy_key", "stale_days"}
        assert pool.store == {}


# ════════════════════════ Недоступная БД при старте ════════════════════════


class TestDbUnavailableAtStartup:
    @pytest.mark.asyncio
    async def test_get_runtime_config_survives_db_failure(self, monkeypatch, caplog):
        """БД недоступна при первом обращении: процесс жив, дефолты в силе,
        повторная попытка привязки на следующем вызове (связка ретраится)."""
        import memory_server.state as state_mod

        st = state_mod.SeltiState()

        async def broken_repo():
            raise ConnectionRefusedError("pg is down")

        # пул создаётся — падает создание репозитория (первая точка отказа)
        monkeypatch.setattr(st, "get_settings_repository", broken_repo)
        runtime = await st.get_runtime_config()
        assert runtime.get("stale_days") == 30  # дефолты
        assert runtime.effective_source("stale_days") == "default"
        # не bound → следующая попытка снова попробует привязать
        assert st._runtime_config_bound is False

        # БД ожила: повторный вызов довязывает (listener не поднимаем —
        # юнит без реального PG; проверяем bind+прогрев+флаг bound)
        pool = FakePool()
        live_repo = SettingsRepository(pool)

        async def live_getter():
            return live_repo

        runtime_quiet_start = st._runtime_config
        monkeypatch.setattr(st, "get_settings_repository", live_getter)

        async def quiet_start():
            await runtime_quiet_start.refresh()

        monkeypatch.setattr(runtime_quiet_start, "start", quiet_start)
        runtime2 = await st.get_runtime_config()
        assert st._runtime_config_bound is True
        assert runtime2.get("stale_days") == 30


# ════════════════════════ Регрессия: дедлок первого вызова ════════════════════════


class TestFirstCallNoDeadlock:
    @pytest.mark.asyncio
    async def test_first_get_runtime_config_no_services_lock_deadlock(self, monkeypatch):
        """Регрессия прод-инцидента 23.09 (деплой fda6e9f): get_runtime_config
        захватывал _services_lock и ВНУТРИ лока звал get_settings_repository(),
        который при первом вызове берёт тот же лок — вложенный захват
        asyncio.Lock (не реентерабельный) висел вечно: 4 uvicorn-воркера
        зависали в lifespan, /live таймаутился, healthcheck красный, воркер
        не поднимался по зависимости. Фикс: репозиторий берётся ДО лока.

        get_settings_repository здесь НЕ мокаем — проверяется настоящий
        вложенный путь на чистом state (в отличие от теста выше, где геттер
        подменён и дедлок не ловится).
        """
        import asyncio

        import memory_server.state as state_mod
        from memory_server.runtime_config import RuntimeConfig

        st = state_mod.SeltiState()

        async def fake_pool():
            return FakePool()

        monkeypatch.setattr(st, "get_pool", fake_pool)

        started: dict[str, bool] = {}

        async def fake_start(self: RuntimeConfig) -> None:
            # listener не поднимаем (юнит без PG); сам факт bind+start
            # фиксируем флагом
            started["bound"] = True

        monkeypatch.setattr(RuntimeConfig, "start", fake_start)

        # на старом коде (repo-вызов внутри лока) зависает → TimeoutError
        runtime = await asyncio.wait_for(st.get_runtime_config(), timeout=3)
        assert st._runtime_config_bound is True
        assert started.get("bound") is True

        # повторный вызов — быстрая ветка (ранний return до лока)
        again = await asyncio.wait_for(st.get_runtime_config(), timeout=3)
        assert again is runtime
