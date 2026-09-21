"""Полная карта 3D (PLAN_FULL_MAP_3D M1/M2) — задачи Celery, очередь memory.

map_meta            — метa снапшота (version, счётчики; кеш Redis 60с)
build_map_snapshot  — холодная сборка снапшота под build-lock, gz-байты
                      в Redis (PLAN: Celery-JSON не переносит байты, web
                      читает Redis сам — как fast-path облачка Фазы 6)
layout_map          — beat: DrL dim=3 + релаксация + bbox, UPSERT map_layout
bump_map_dirty      — инвалидатор кешей после reconciler/refresh_clusters
"""

from typing import Any

from celery import shared_task

from memory_server.logger import get_logger
from memory_server.state import get_state
from memory_server.tasks.async_bridge import run_async
from memory_server.tasks.base import SeltiTask

logger = get_logger(__name__)


def _get_map_service():
    """MapService via process-wide SeltiState (composition root)."""
    return run_async(get_state().get_map_service)


def bump_map_dirty() -> None:
    """Снести кеш карты после изменения рёбер/кластеров (best-effort).

    Вызывают name_reconciler/co_occurrence (linker_tasks) и refresh_clusters
    (lifecycle_tasks): dirty-флаг + DEL map:snap:* → первый /api/map/full
    пересоберёт снапшот. Сбой инвалидации не роняет кампанию-носитель —
    максимум карта протухнет до смены version-хэша.
    """
    try:
        run_async(_get_map_service().bump_dirty)
    except Exception as exc:
        logger.warning("map: dirty bump failed (non-fatal)", extra={"error": str(exc)[:200]})


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.map_tasks.map_meta",
    soft_time_limit=10,
    time_limit=30,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def map_meta(self) -> dict[str, Any]:
    """Мета карты: version + счётчики + layout_stale (кеш Redis 60с, <50мс)."""
    service = _get_map_service()
    meta = run_async(service.meta)
    meta["layout_stale"] = run_async(service.layout_stale, meta["version"])
    return meta


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.map_tasks.build_map_snapshot",
    max_retries=2,
    retry_backoff=True,
    retry_backoff_max=60,
    default_retry_delay=10,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def build_map_snapshot(
    self,
    with_preview: bool = True,
    project_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Собрать снапшот (miss кеша) и положить gz-байты в Redis.

    Возвращает отчёт с ключом map:snap:... — web-слой отдаёт байты из
    Redis напрямую. build-lock: конкурент с тем же suffix ждёт ключ,
    а не дублирует сборку.
    """
    return run_async(
        _get_map_service().ensure_snapshot,
        with_preview=with_preview,
        project_id=project_id,
        namespace=namespace,
    )


@shared_task(
    bind=True,
    base=SeltiTask,
    name="memory_server.tasks.map_tasks.layout_map",
    max_retries=2,
    retry_backoff=True,
    retry_backoff_max=60,
    default_retry_delay=30,
    soft_time_limit=240,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
    queue="memory",
    routing_key="memory",
)
def layout_map(self) -> dict[str, Any]:
    """Пересчёт 3D-раскладки (beat, 02:30 UTC — после refresh_clusters)."""
    return run_async(_get_map_service().rebuild_layout)
