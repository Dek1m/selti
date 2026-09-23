"""Юнит-тесты реестра настроек и SettingsRepository (Ф2).

FakeConn имитирует asyncpg-соединение (dict-строки вместо Record):
UPSERT/DELETE семантика репозитория проверяется без PostgreSQL.
"""

import json

import asyncpg
from datetime import datetime, timezone

import pytest

from memory_server.settings_store import (
    REGISTRY,
    SCHEDULE_KEYS,
    GROUPS,
    ProfileError,
    ProfileNotFoundError,
    SettingsConfirmationError,
    SettingsLockedError,
    SettingsRepository,
    SettingsValidationError,
    get_default,
    validate_linker_invariant,
    validate_value,
)


# ════════════════════════ Мок-инфраструктура ════════════════════════


class FakeConn:
    """Минимальный asyncpg-протокол для SettingsRepository."""

    def __init__(self, store: dict, profiles: dict, sequence: list) -> None:
        self._store = store  # key → dict-row
        self._profiles = profiles
        self._sequence = sequence
        self._next_id = max(profiles.keys(), default=0) + 1

    def _full_setting_row(self, key: str) -> dict:
        from memory_server.settings_store import REGISTRY as registry

        row = self._store.get(key, {})
        spec = registry.get(key)
        if spec is None:
            # Строка вне реестра (ключ удалён из кода, но жив в БД):
            # реальный _row_to_record такие переносит — мок тоже должен
            return {
                "key": key, "value": row.get("value"), "value_type": "json",
                "group_key": "legacy", "title_ru": None, "description_ru": None,
                "is_dangerous": False, "requires_restart": False,
                "updated_at": row.get("updated_at"), "updated_by": "seed",
            }
        return {
            "key": key,
            "value": row.get("value"),
            "value_type": spec.value_type,
            "group_key": spec.group,
            "title_ru": row.get("title_ru"),
            "description_ru": row.get("description_ru"),
            "is_dangerous": spec.dangerous,
            "requires_restart": spec.requires_restart,
            "updated_at": row.get("updated_at"),
            "updated_by": row.get("updated_by", "seed"),
        }

    async def execute(self, sql: str, *args) -> str:
        self._sequence.append(("execute", sql, args))
        if "INSERT INTO app_settings" in sql:
            # jsonb-параметры приходят python-объектами (кодек пула);
            # порядок _upsert_args: key, value, vtype, group, title, desc, ...
            key, value = args[0], args[1]
            row = self._store.get(key, {})
            row.update(
                {"key": key, "value": value, "updated_by": args[-1],
                 "title_ru": args[4], "description_ru": args[5],
                 "updated_at": datetime.now(timezone.utc)}
            )
            self._store[key] = row
            return "INSERT 0 1"
        if "DELETE FROM app_settings WHERE key = $1" in sql:
            self._store.pop(args[0], None)
            return "DELETE 1"
        if "UPDATE app_settings_profiles SET applied_at" in sql:
            self._profiles[args[0]]["applied_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"
        if "DELETE FROM app_settings_profiles WHERE id = $1" in sql:
            self._profiles.pop(args[0], None)
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {sql}")

    async def executemany(self, sql: str, seq) -> None:
        for args in seq:
            await self.execute(sql, *args)

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    async def fetch(self, sql: str, *args) -> list[dict]:
        self._sequence.append(("fetch", sql, args))
        if "FROM app_settings_profiles" in sql:
            return list(self._profiles.values())
        return [self._full_setting_row(key) for key in self._store]

    async def fetchrow(self, sql: str, *args) -> dict | None:
        self._sequence.append(("fetchrow", sql, args))
        if "INSERT INTO app_settings_profiles" in sql:
            if any(p["name"] == args[0] for p in self._profiles.values()):
                raise asyncpg.UniqueViolationError()
            row = {
                "id": self._next_id, "name": args[0], "description": args[1],
                "is_builtin": False, "created_at": datetime.now(timezone.utc),
                "applied_at": None, "values": args[2],
            }
            self._profiles[self._next_id] = row
            self._next_id += 1
            return row
        if "UPDATE app_settings_profiles" in sql:
            row = self._profiles[args[0]]
            row["values"] = args[1]
            if args[2] is not None:
                row["description"] = args[2]
            return row
        if "FROM app_settings_profiles WHERE" in sql:
            return self._profiles.get(args[0])
        row = self._store.get(args[0])
        return self._full_setting_row(args[0]) if row is not None else None


class FakePool:
    def __init__(self) -> None:
        self.store: dict = {}
        self.profiles: dict = {}
        self.calls: list = []

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return FakeConn(pool.store, pool.profiles, pool.calls)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def make_repo(env_locked: set[str] | None = None) -> tuple[SettingsRepository, FakePool]:
    pool = FakePool()
    return SettingsRepository(pool, env_locked_keys=env_locked or set()), pool


# ════════════════════════ Реестр ════════════════════════


class TestRegistry:
    def test_97_keys(self):
        assert len(REGISTRY) == 97

    def test_groups_cover_all_keys(self):
        for spec in REGISTRY.values():
            assert spec.group in GROUPS, spec.key

    def test_dangerous_keys_are_8(self):
        dangerous = {k for k, s in REGISTRY.items() if s.dangerous}
        assert dangerous == {
            "dedup_enabled", "gc_purge_enabled", "gc_mode", "gc_retention_days",
            "linker_enabled", "linker_reconciler_dry_run",
            "edge_lifecycle_enabled", "edge_prune_dry_run",
        }

    def test_celery_and_schedule_restart_concurrency_live(self):
        restart = {k for k, s in REGISTRY.items() if s.requires_restart}
        assert len(SCHEDULE_KEYS) == 13
        assert SCHEDULE_KEYS <= restart
        # concurrency применяется налету (pool_grow/shrink) — не рестарт
        assert REGISTRY["celery_worker_concurrency"].requires_restart is False
        assert REGISTRY["celery_worker_prefetch_multiplier"].requires_restart is True

    def test_defaults_match_settings_fields(self):
        from memory_server.config import settings

        for key, spec in REGISTRY.items():
            if key.startswith("schedule."):
                continue
            assert getattr(settings, key) == get_default(key), key

    def test_schedule_defaults_shape(self):
        for key in SCHEDULE_KEYS:
            default = get_default(key)
            assert default["type"] in ("interval", "crontab"), key
            if default["type"] == "interval":
                assert 10 <= default["seconds"] <= 604800


# ════════════════════════ Валидация значений ════════════════════════


class TestValidateValue:
    def test_int_range(self):
        assert validate_value("hybrid_prefetch", 50) == 50
        with pytest.raises(SettingsValidationError):
            validate_value("hybrid_prefetch", 5)
        with pytest.raises(SettingsValidationError):
            validate_value("hybrid_prefetch", 1001)

    def test_bool_strict(self):
        assert validate_value("dedup_enabled", True) is True
        with pytest.raises(SettingsValidationError):
            validate_value("dedup_enabled", "yes")
        with pytest.raises(SettingsValidationError):
            validate_value("dedup_enabled", 1)  # bool — не int

    def test_float_coercion(self):
        assert validate_value("mmr_lambda", 1) == 1.0

    def test_enum(self):
        assert validate_value("gc_mode", "soft") == "soft"
        with pytest.raises(SettingsValidationError):
            validate_value("gc_mode", "nuclear")

    def test_unknown_key(self):
        with pytest.raises(SettingsValidationError):
            validate_value("no_such_key", 1)

    def test_namespace_dict_requires_default(self):
        with pytest.raises(SettingsValidationError):
            validate_value("dedup_thresholds", {"code_knowledge": 0.9})
        value = validate_value("dedup_thresholds", {"default": 0.95, "code_knowledge": 0.9})
        assert value == {"default": 0.95, "code_knowledge": 0.9}
        with pytest.raises(SettingsValidationError):
            validate_value("recency_decay_rates", {"default": 0.5})  # вне 0.9..1.0

    def test_symmetric_types_whitelist(self):
        assert validate_value("traverse_symmetric_link_types", ["related_to"]) == ["related_to"]
        with pytest.raises(SettingsValidationError):
            validate_value("traverse_symmetric_link_types", ["supersedes"])

    def test_schedule_interval(self):
        ok = {"type": "interval", "seconds": 120}
        assert validate_value("schedule.rebuild_contexts", ok) == ok
        with pytest.raises(SettingsValidationError):
            validate_value("schedule.rebuild_contexts", {"type": "interval", "seconds": 5})
        with pytest.raises(SettingsValidationError):
            validate_value("schedule.rebuild_contexts", {"type": "hourly"})

    def test_schedule_crontab_validated_by_celery(self):
        ok = {"type": "crontab", "minute": "0", "hour": "4"}
        assert validate_value("schedule.mark_stale", ok) == ok
        with pytest.raises(SettingsValidationError):
            validate_value("schedule.mark_stale", {"type": "crontab", "minute": "abc"})

    def test_llm_base_url(self):
        assert validate_value("linker_llm_base_url", "") == ""
        assert validate_value("linker_llm_base_url", "https://api.example.com/v1") == "https://api.example.com/v1"
        with pytest.raises(SettingsValidationError):
            validate_value("linker_llm_base_url", "ftp://x")


class TestLinkerInvariant:
    def test_default_state_passes(self):
        validate_linker_invariant({})

    def test_synonym_ge_verdict_fails(self):
        with pytest.raises(SettingsValidationError):
            validate_linker_invariant({"linker_synonym_threshold": 0.86})

    def test_verdict_ge_dedup_ns_fails(self):
        with pytest.raises(SettingsValidationError):
            validate_linker_invariant(
                {
                    "linker_synonym_threshold": 0.80,
                    "linker_verdict_threshold": 0.87,  # ≥ dialogue_insights 0.85
                }
            )


# ════════════════════════ Repository ════════════════════════


class TestRepositorySet:
    @pytest.mark.asyncio
    async def test_set_and_load(self):
        repo, pool = make_repo()
        record = await repo.set("hybrid_prefetch", 200)
        assert record.value == 200
        records = await repo.load_all()
        assert records["hybrid_prefetch"].value == 200

    @pytest.mark.asyncio
    async def test_dangerous_requires_confirm(self):
        repo, _ = make_repo()
        with pytest.raises(SettingsConfirmationError) as exc_info:
            await repo.set("gc_purge_enabled", True)
        assert exc_info.value.keys == ["gc_purge_enabled"]
        record = await repo.set("gc_purge_enabled", True, confirm=True)
        assert record.value is True

    @pytest.mark.asyncio
    async def test_env_locked_rejected(self):
        repo, _ = make_repo(env_locked={"dedup_enabled"})
        with pytest.raises(SettingsLockedError):
            await repo.set("dedup_enabled", False, confirm=True)

    @pytest.mark.asyncio
    async def test_validation_error_before_write(self):
        repo, pool = make_repo()
        with pytest.raises(SettingsValidationError):
            await repo.set("rrf_k", 99999)
        assert pool.store == {}

    @pytest.mark.asyncio
    async def test_invariant_checked_against_effective(self):
        repo, _ = make_repo()
        # текущий verdict 0.85; synonym 0.85 ломает инвариант
        with pytest.raises(SettingsValidationError):
            await repo.set("linker_synonym_threshold", 0.85, effective={})
        # 0.84 < 0.85 — ок
        await repo.set("linker_synonym_threshold", 0.84, effective={})


class TestUpsertFullMetadata:
    """Решение Мастера (компромисс Ф4, вариант «тексты в реестр кода»):
    reset→PUT (и apply_profile) пересоздают строку с ПОЛНЫМИ метаданными —
    title_ru/description_ru записаны в БД, а не отданы фолбэком."""

    @pytest.mark.asyncio
    async def test_reset_then_put_recreates_row_with_ru_texts(self):
        repo, pool = make_repo()
        await repo.set("stale_days", 45)
        await repo.reset("stale_days")
        assert "stale_days" not in pool.store  # строка удалена

        record = await repo.set("stale_days", 50)  # пересоздание после reset
        # тексты записаны В СТРОКУ (сырой store, не фолбэк чтения)
        row = pool.store["stale_days"]
        assert row["title_ru"] == "Дней без доступа"
        assert row["description_ru"].startswith("Сколько дней гранула")
        # и прочитаны из БД в запись ответа
        assert record.title_ru == "Дней без доступа"
        assert record.description_ru

    @pytest.mark.asyncio
    async def test_upsert_args_carry_full_metadata(self):
        from memory_server.settings_store import _upsert_args

        args = _upsert_args("gc_mode", "hard", "ui")
        title, description = args[4], args[5]
        assert title == "Режим GC"
        assert "disabled" in description
        # остальные поля метаданных — на своих местах
        assert args[2] == "str" and args[3] == "lifecycle"
        assert args[10] is True  # dangerous
        assert args[-1] == "ui"

    @pytest.mark.asyncio
    async def test_apply_profile_recreates_rows_with_ru_texts(self):
        repo, pool = make_repo()
        pool.store["rrf_k"] = {"value": 60}  # строка без текстов (легаси)
        pool.profiles[1] = {
            "id": 1, "name": "p", "description": None, "is_builtin": False,
            "created_at": None, "applied_at": None, "values": {"rrf_k": 90},
        }
        await repo.apply_profile(1, confirm=True)
        assert pool.store["rrf_k"]["title_ru"] == "Коэффициент RRF"
        assert pool.store["rrf_k"]["description_ru"]


class TestRepositoryReset:
    @pytest.mark.asyncio
    async def test_reset_removes_row(self):
        repo, pool = make_repo()
        await repo.set("stale_days", 60)
        await repo.reset("stale_days")
        assert "stale_days" not in pool.store

    @pytest.mark.asyncio
    async def test_reset_dangerous_requires_confirm(self):
        repo, pool = make_repo()
        await repo.set("gc_mode", "hard", confirm=True)
        with pytest.raises(SettingsConfirmationError):
            await repo.reset("gc_mode")
        await repo.reset("gc_mode", confirm=True)
        assert pool.store == {}

    @pytest.mark.asyncio
    async def test_reset_all_dangerous_without_confirm_nothing_deleted(self):
        repo, pool = make_repo()
        await repo.set("stale_days", 60)
        await repo.set("gc_mode", "soft", confirm=True)
        with pytest.raises(SettingsConfirmationError) as exc_info:
            await repo.reset_all()
        assert exc_info.value.keys == ["gc_mode"]
        assert len(pool.store) == 2  # ничего не сброшено

    @pytest.mark.asyncio
    async def test_reset_all_with_confirm(self):
        repo, pool = make_repo()
        await repo.set("stale_days", 60)
        await repo.set("gc_mode", "soft", confirm=True)
        reset = await repo.reset_all(confirm=True)
        assert set(reset) == {"stale_days", "gc_mode"}
        assert pool.store == {}

    @pytest.mark.asyncio
    async def test_reset_all_skips_env_locked(self):
        repo, pool = make_repo(env_locked={"stale_days"})
        # строка могла остаться с прошлых времён (env включили позже)
        pool.store["stale_days"] = {"value": 60}
        await repo.set("rrf_k", 10)
        reset = await repo.reset_all(confirm=True)
        assert reset == ["rrf_k"]
        assert "stale_days" in pool.store


class TestProfiles:
    @pytest.mark.asyncio
    async def test_create_list_update_delete(self):
        repo, pool = make_repo()
        profile = await repo.create_profile("night-shift", "ночной режим", {"stale_days": 14})
        assert profile.values == {"stale_days": 14}
        listed = await repo.list_profiles()
        assert [p.name for p in listed] == ["night-shift"]
        await repo.update_profile(profile.id, {"rrf_k": 30}, description="обновлён")
        with pytest.raises(ProfileNotFoundError):
            await repo.delete_profile(999)

    @pytest.mark.asyncio
    async def test_duplicate_name_conflict(self):
        repo, _ = make_repo()
        await repo.create_profile("dup", None, {})
        with pytest.raises(ProfileError):
            await repo.create_profile("dup", None, {})

    @pytest.mark.asyncio
    async def test_builtin_protected(self):
        repo, pool = make_repo()
        pool.profiles[1] = {
            "id": 1, "name": "factory", "description": None, "is_builtin": True,
            "created_at": None, "applied_at": None, "values": {"stale_days": 30},
        }
        with pytest.raises(ProfileError):
            await repo.delete_profile(1)
        with pytest.raises(ProfileError):
            await repo.update_profile(1, {})

    @pytest.mark.asyncio
    async def test_apply_atomic_and_reports(self):
        repo, pool = make_repo()
        profile = await repo.create_profile("tune", None, {"stale_days": 14, "rrf_k": 30})
        report = await repo.apply_profile(profile.id)
        assert report == {"applied": ["rrf_k", "stale_days"], "skipped_env": []}
        assert pool.store["stale_days"]["value"] == 14

    @pytest.mark.asyncio
    async def test_apply_dangerous_requires_confirm(self):
        repo, _ = make_repo()
        profile = await repo.create_profile("danger", None, {"gc_mode": "hard"})
        with pytest.raises(SettingsConfirmationError) as exc_info:
            await repo.apply_profile(profile.id)
        assert exc_info.value.keys == ["gc_mode"]

    @pytest.mark.asyncio
    async def test_apply_skips_env_locked(self):
        repo, _ = make_repo(env_locked={"stale_days"})
        profile = await repo.create_profile("mixed", None, {"stale_days": 7, "rrf_k": 20})
        report = await repo.apply_profile(profile.id)
        assert report["skipped_env"] == ["stale_days"]
        assert report["applied"] == ["rrf_k"]

    @pytest.mark.asyncio
    async def test_apply_invalid_value_rejects_whole_profile(self):
        repo, pool = make_repo()
        profile = await repo.create_profile("broken", None, {"rrf_k": 20, "mmr_lambda": 99})
        with pytest.raises(SettingsValidationError):
            await repo.apply_profile(profile.id)
        assert pool.store == {}  # атомарность: ничего не применилось
