"""Тестовая задача для интеграционного теста моста (subprocess celery worker).

Импортируется живым воркером через `--include=tests.integration_bridge_worker`
(см. tests/test_bridge_integration.py). Регистрирует задачу с проверяемым
результатом и без внешних зависимостей (PG/Qdrant не нужны) — изоляция
теста моста от инфраструктуры данных: проверяется путь
headers → prerun → postrun → LPUSH → BLPOP, а не бизнес-логика.

НЕ собирается pytest'ом (нет test_ префикса).
"""

from memory_server.celery_app import app

# Задержка echo: тест (б) требует, чтобы ожидание завершилось именно
# BLPOP-событием, а не ready-страховкой. Без сна возникает гонка «воркер
# выполнил задачу раньше первого result.ready()» → while в task_bridge
# не входит в BLPOP → notify-ключ остаётся (не потреблён, гаснет по TTL).
# 0.7с > времени до первого ready()-чека, < BRIDGE_LATENCY_LIMIT 2с.
_ECHO_DELAY_SECONDS = 0.7


@app.task(name="integration.bridge_echo")
def bridge_echo(payload: str = "") -> dict:
    """Вернуть payload и task_id — сверка результата с AsyncResult.id."""
    import time

    time.sleep(_ECHO_DELAY_SECONDS)
    return {"echo": payload, "task_id": app.current_task.request.id}
