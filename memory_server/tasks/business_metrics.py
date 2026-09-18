"""Periodic сбор бизнес-метрик.

Обновляет MEMORY_GROWTH_RATE (records/hour per namespace).
Вызывается через Beat schedule раз в час.

Алгоритм:
1. Запоминаем count per namespace при первом запуске (snapshot_0)
2. При следующем запуске берём текущий count (snapshot_1)
3. growth_rate = (snapshot_1 - snapshot_0) / elapsed_hours
4. snapshot_0 = snapshot_1 для следующего цикла
"""

import logging
import time

from memory_server.celery_app import app
from memory_server.metrics import MEMORY_GROWTH_RATE

logger = logging.getLogger(__name__)

# Предыдущий snapshot: {namespace: (count, timestamp)}
_prev_snapshot: dict[str, tuple[int, float]] = {}


@app.task(name="business_metrics.update")
def update_business_metrics():
    """Обновить бизнес-метрики: growth rate per namespace."""
    try:
        # Синхронный запрос через run_async bridge
        from memory_server.state import get_state
        from memory_server.tasks.async_bridge import run_async

        pool = run_async(get_state().get_pool)

        async def _fetch_counts() -> dict[str, int]:
            # Актуальные гранулы = status='asserted' AND valid_to IS NULL (миграция 018);
            # namespace_id → uid через JOIN (TEXT-колонка дропается 018c)
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT n.uid AS namespace, COUNT(*) AS cnt "
                    "FROM memories m JOIN namespaces n ON n.id = m.namespace_id "
                    "WHERE m.status = 'asserted' AND m.valid_to IS NULL "
                    "GROUP BY n.uid"
                )
                return {row["namespace"]: row["cnt"] for row in rows}

        counts = run_async(_fetch_counts)
        now = time.time()

        for namespace, count in counts.items():
            if namespace in _prev_snapshot:
                prev_count, prev_time = _prev_snapshot[namespace]
                elapsed_hours = (now - prev_time) / 3600.0
                if elapsed_hours > 0:
                    rate = (count - prev_count) / elapsed_hours
                    MEMORY_GROWTH_RATE.labels(namespace=namespace).set(round(rate, 2))
            _prev_snapshot[namespace] = (count, now)

        # Обнуляем rate для namespace, которых больше нет в БД
        for ns in list(_prev_snapshot.keys()):
            if ns not in counts:
                MEMORY_GROWTH_RATE.labels(namespace=ns).set(0)
                del _prev_snapshot[ns]

        logger.info(
            "business_metrics: updated",
            extra={"namespaces": len(counts), "counts": counts},
        )
    except Exception as e:
        logger.warning("business_metrics: failed", extra={"error": str(e)[:200]})
