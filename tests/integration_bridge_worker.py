"""Тестовая задача для интеграционного теста моста (subprocess celery worker).

Импортируется живым воркером через `--include=tests.integration_bridge_worker`
(см. tests/test_bridge_integration.py). Регистрирует задачу с проверяемым
результатом и без внешних зависимостей (PG/Qdrant не нужны) — изоляция
теста моста от инфраструктуры данных: проверяется путь
headers → prerun → postrun → LPUSH → BLPOP, а не бизнес-логика.

НЕ собирается pytest'ом (нет test_ префикса).
"""

from memory_server.celery_app import app


@app.task(name="integration.bridge_echo")
def bridge_echo(payload: str = "") -> dict:
    """Вернуть payload и task_id — сверка результата с AsyncResult.id."""
    return {"echo": payload, "task_id": app.current_task.request.id}
