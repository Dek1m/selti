"""REST API конфигурации (Ф2): /api/settings + профили.

Контракт — docs/SETTINGS_REGISTRY.md. GET отдаёт все 97 runtime-ключей
(схема из реестра кода + value/updated_* из БД); секреты/фундамент не
приходят никогда — их нет в app_settings. PUT/reset/reset-all/apply —
через SettingsRepository (валидация, dangerous-confirm, инвариант
линкера); после записи снапшот web-процесса обновляется сразу, остальные
процессы — через pg NOTIFY. celery_worker_concurrency применяется налету
(pool_grow/pool_shrink broadcast).
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from memory_server.logger import get_logger
from memory_server.runtime_config import RuntimeConfig
from memory_server.settings_store import (
    GROUPS,
    REGISTRY,
    ProfileError,
    ProfileNotFoundError,
    SettingRecord,
    SettingsConfirmationError,
    SettingsLockedError,
    SettingsRepository,
    SettingsValidationError,
    get_default,
)
from memory_server.state import get_state

logger = get_logger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingOut(BaseModel):
    key: str
    group: str
    value_type: str
    widget: str
    value: Any
    db_value: Any | None = None
    default_value: Any = None
    effective_source: Literal["env", "db", "default"]
    differs_from_default: bool
    is_env_locked: bool
    is_dangerous: bool
    requires_restart: bool
    min_value: float | None = None
    max_value: float | None = None
    enum_values: list[str] | None = None
    title_ru: str | None = None
    description_ru: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None


class SettingUpdate(BaseModel):
    value: Any
    confirm: bool = False


class ResetAllRequest(BaseModel):
    confirm: bool = False


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class ProfileUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=500)


class ProfileApply(BaseModel):
    confirm: bool = False


class ProfileOut(BaseModel):
    id: int
    name: str
    description: str | None
    is_builtin: bool
    created_at: datetime | None
    applied_at: datetime | None
    values: dict[str, Any]


# ════════════════════════ Хелперы ════════════════════════


async def _repository() -> SettingsRepository:
    return await get_state().get_settings_repository()


async def _runtime() -> RuntimeConfig:
    return await get_state().get_runtime_config()


def _setting_out(key: str, record: SettingRecord | None, runtime: RuntimeConfig) -> SettingOut:
    """Ответ по одному ключу: схема реестра + БД-метаданные + effective."""
    spec = REGISTRY[key]
    value = runtime.get(key)
    source = runtime.effective_source(key)
    return SettingOut(
        key=key,
        group=spec.group,
        value_type=spec.value_type,
        widget=spec.widget,
        value=value,
        db_value=record.value if record else None,
        default_value=record.default_value if record else get_default(key),
        effective_source=source,
        differs_from_default=value != get_default(key),
        is_env_locked=source == "env",
        is_dangerous=spec.dangerous,
        requires_restart=spec.requires_restart,
        min_value=spec.min_value,
        max_value=spec.max_value,
        enum_values=list(spec.enum_values) if spec.enum_values else None,
        # Подписи — всегда непустые: БД-строка (сидинг/upsert) → реестр кода
        # (строка отсутствует после reset или текст пуст — берём из SettingSpec)
        title_ru=(record.title_ru if record and record.title_ru else spec.title_ru),
        description_ru=(
            record.description_ru if record and record.description_ru else spec.description_ru
        ),
        updated_at=record.updated_at if record else None,
        updated_by=record.updated_by if record else None,
    )


def _map_store_error(exc: Exception) -> HTTPException:
    """Хелпер-маппинг исключений store на HTTP (единая точка)."""
    if isinstance(exc, SettingsValidationError):
        return HTTPException(status_code=400, detail={"message": str(exc), "errors": exc.errors})
    if isinstance(exc, SettingsConfirmationError):
        return HTTPException(status_code=409, detail={"message": str(exc), "keys": exc.keys})
    if isinstance(exc, SettingsLockedError):
        return HTTPException(status_code=409, detail={"message": str(exc)})
    if isinstance(exc, ProfileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ProfileError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _broadcast_concurrency(old: int, new: int) -> None:
    """Налетное применение celery_worker_concurrency: pool_grow/shrink.

    Best-effort: воркер офлайн применит значение при своём рестарте
    (celeryd_init читает БД). Не ждём ответов — управление не блокирует.
    """
    if old == new:
        return
    try:
        from memory_server.celery_app import app

        if new > old:
            app.control.broadcast("pool_grow", arguments={"n": new - old})
        else:
            app.control.broadcast("pool_shrink", arguments={"n": old - new})
        logger.info("settings: worker concurrency broadcast", extra={"old": old, "new": new})
    except Exception as exc:
        logger.warning(
            "settings: concurrency broadcast failed (applies on worker restart)",
            extra={"error": str(exc)[:200], "old": old, "new": new},
        )


async def _after_write(runtime: RuntimeConfig, concurrency_changed: bool = False) -> None:
    """Пост-запись: снапшот web-процесса сразу; остальные — через NOTIFY."""
    old_concurrency = runtime.get("celery_worker_concurrency") if concurrency_changed else None
    await runtime.refresh()
    if concurrency_changed and old_concurrency is not None:
        _broadcast_concurrency(int(old_concurrency), int(runtime.get("celery_worker_concurrency")))


# ════════════════════════ Настройки ════════════════════════


@router.get("")
@router.get("/")
async def list_settings() -> dict[str, Any]:
    """Все runtime-ключи: метаданные + value + effective_source +
    differs_from_default. Секретов/фундамента здесь нет в принципе."""
    repo = await _repository()
    runtime = await _runtime()
    try:
        records = await repo.load_all()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"app_settings unavailable: {exc}") from exc
    items = [_setting_out(key, records.get(key), runtime) for key in REGISTRY]
    groups = [{"key": key, "title_ru": title} for key, title in GROUPS.items()]
    return {"settings": items, "groups": groups}


@router.post("/reset-all")
async def reset_all_settings(req: ResetAllRequest) -> dict[str, Any]:
    """Сброс всех БД-переопределений. Опасные ключи без confirm → 409
    со списком (ничего не сбрасывается). Env-ключи не трогаются."""
    repo = await _repository()
    runtime = await _runtime()
    try:
        reset = await repo.reset_all(confirm=req.confirm)
    except SettingsConfirmationError as exc:
        raise _map_store_error(exc) from exc
    logger.info("settings: reset-all", extra={"count": len(reset)})
    await _after_write(runtime)
    return {"reset": reset}


# ════════════════════════ Профили ════════════════════════
# ВАЖНО: статические префиксы (/profiles, /reset-all) объявлены ДО
# параметрического /{key} — иначе FastAPI сматчит "profiles" как key.

@router.get("/profiles")
@router.get("/profiles/")
async def list_profiles() -> dict[str, Any]:
    repo = await _repository()
    try:
        profiles = await repo.list_profiles()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"app_settings_profiles unavailable: {exc}") from exc
    return {"profiles": [ProfileOut(**p.__dict__) for p in profiles]}


@router.post("/profiles", status_code=201)
async def create_profile(req: ProfileCreate) -> ProfileOut:
    """Снапшот ТЕКУЩИХ effective-значений всех runtime-ключей."""
    runtime = await _runtime()
    repo = await _repository()
    try:
        profile = await repo.create_profile(req.name, req.description, runtime.snapshot())
    except ProfileError as exc:
        raise _map_store_error(exc) from exc
    return ProfileOut(**profile.__dict__)


@router.put("/profiles/{profile_id}")
async def refresh_profile(profile_id: int, req: ProfileUpdate | None = None) -> ProfileOut:
    """Перезаписать снапшот профиля текущими effective-значениями."""
    runtime = await _runtime()
    repo = await _repository()
    try:
        profile = await repo.update_profile(
            profile_id, values=runtime.snapshot(), description=req.description if req else None
        )
    except (ProfileError, ProfileNotFoundError) as exc:
        raise _map_store_error(exc) from exc
    return ProfileOut(**profile.__dict__)


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: int) -> dict[str, Any]:
    repo = await _repository()
    try:
        await repo.delete_profile(profile_id)
    except (ProfileError, ProfileNotFoundError) as exc:
        raise _map_store_error(exc) from exc
    return {"deleted": profile_id}


@router.post("/profiles/{profile_id}/apply")
async def apply_profile(profile_id: int, req: ProfileApply) -> dict[str, Any]:
    """Атомарно применить профиль. Dangerous без confirm → 409 со списком;
    env-ключи пропускаются (env жёстко сильнее) — в отчёте skipped_env."""
    repo = await _repository()
    runtime = await _runtime()
    try:
        report = await repo.apply_profile(
            profile_id, confirm=req.confirm, updated_by="ui", effective=runtime.snapshot()
        )
    except (ProfileError, ProfileNotFoundError, SettingsValidationError, SettingsConfirmationError) as exc:
        raise _map_store_error(exc) from exc
    await _after_write(runtime, concurrency_changed=True)
    logger.info(
        "settings: profile applied",
        extra={"profile_id": profile_id, "applied": len(report["applied"])},
    )
    return report


# ════════════════════════ Одиночные ключи ════════════════════════


@router.get("/{key}")
async def get_setting(key: str) -> SettingOut:
    repo = await _repository()
    runtime = await _runtime()
    if key not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown setting key: {key}")
    record = await repo.get(key)
    return _setting_out(key, record, runtime)


@router.put("/{key}")
async def update_setting(key: str, req: SettingUpdate) -> SettingOut:
    """Записать значение: 400 валидация | 409 dangerous без confirm /
    env-блокировка | 200 обновлённая запись."""
    if key not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown setting key: {key}")
    repo = await _repository()
    runtime = await _runtime()
    try:
        await repo.set(key, req.value, confirm=req.confirm, updated_by="ui", effective=runtime.snapshot())
    except (SettingsValidationError, SettingsConfirmationError, SettingsLockedError) as exc:
        raise _map_store_error(exc) from exc
    await _after_write(runtime, concurrency_changed=key == "celery_worker_concurrency")
    record = await repo.get(key)
    return _setting_out(key, record, runtime)


@router.post("/{key}/reset")
async def reset_setting(key: str, req: ResetAllRequest) -> SettingOut:
    """Сброс к дефолту: строка удаляется из БД (dangerous — с confirm)."""
    if key not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown setting key: {key}")
    repo = await _repository()
    runtime = await _runtime()
    try:
        await repo.reset(key, confirm=req.confirm)
    except (SettingsValidationError, SettingsConfirmationError, SettingsLockedError) as exc:
        raise _map_store_error(exc) from exc
    await _after_write(runtime, concurrency_changed=key == "celery_worker_concurrency")
    return _setting_out(key, None, runtime)
