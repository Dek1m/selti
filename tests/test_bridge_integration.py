"""Интеграционный тест event-driven моста на ЖИВОМ celery worker (Фаза 3.3).

Закрывает дыру юнит-тестов: там сигнал task_prerun диспатчился вручную с
headers в kwargs сигнала — реальный Celery 5 так НЕ делает (send_prerun в
celery/app/trace.py передаёт только sender/task_id/task/args/kwargs, headers
доступны исключительно через task.request). Из-за этого on_task_prerun не
регистрировал _bridge_waiters → on_task_postrun не публиковал LPUSH
selti:bridge:done:{task_id} → мост выжидал 30-секундный BLPOP-чанк на каждый
вызов, спасала страховка result.ready() (прод-инцидент, откат на 4a9d6d4).

Здесь полный живой путь, без моков:
    redis-server (выделенный порт, tmp dir)
      → subprocess celery worker (--pool=solo, env на тестовый Redis)
      → run_task с headers bridge_wait (как в task_bridge.run_task)
      → настоящие сигналы prerun/postrun в процессе воркера
      → LPUSH события → BLPOP будит мост.

Проверки:
    (а) latency ≤ 2с (баг-версия дала бы ~30с — весь чанк BLPOP);
    (б) notify-ключ публикуется (prerun увидел bridge_wait) и гаснет
        (BLPOP потребил элемент);
    (в) результат содержателен и корректен (echo + task_id).

Redis обязан быть настоящий (fakeredis не обслужит BLPOP между двумя
процессами): redis-server ищется в PATH (scoop/choco/нативный), при
отсутствии — pytest.skip со сценой для ручного стенда.
"""

import gc
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

redis_sync = pytest.importorskip("redis")

from celery import Celery

from memory_server.tools.task_bridge import NOTIFY_KEY_PREFIX, run_task

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Название echo-задачи (tests/integration_bridge_worker.py, --include воркера)
ECHO_TASK = "integration.bridge_echo"

WORKER_READY_TIMEOUT = 60.0
BRIDGE_LATENCY_LIMIT = 2.0
TASK_DEADLINE = 20.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _redis_url(port: int) -> str:
    return f"redis://127.0.0.1:{port}/0"


# ── Живой redis-server ────────────────────────────────────────────


@pytest.fixture(scope="module")
def redis_server():
    """Настоящий redis-server: BLPOP/LPUSH между процессами живьём."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip(
            "redis-server not found in PATH — real Redis required for "
            "BLPOP bridge (fakeredis is not usable across processes)"
        )

    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="selti-bridge-redis-")
    proc = subprocess.Popen(
        [
            binary,
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
            "--dir", data_dir,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis_sync.Redis(
        host="127.0.0.1", port=port, decode_responses=True,
        socket_connect_timeout=2.0,
    )
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail("redis-server exited during startup")
            try:
                if client.ping():
                    break
            except redis_sync.ConnectionError:
                time.sleep(0.1)
        else:
            pytest.fail("redis-server did not answer PING in 10s")
        yield {"port": port, "client": client}
    finally:
        # Похоронить AsyncResult'ы тестов ДО смерти redis: их __del__ рвёт
        # pubsub-подписку, а по мёртвому redis celери-дрейнер уходит в
        # reconnect-цикл и сыплет unraisable-исключением в чужие тесты
        gc.collect()
        proc.terminate()
        proc.wait(timeout=10)


# ── Живой celery worker (subprocess, solo pool) ────────────────────


@pytest.fixture(scope="module")
def celery_worker(redis_server):
    """Живой воркер на тестовом Redis: настоящие сигналы, настоящий backend.

    env (REDIS_URL / CELERY_BROKER_URL / CELERY_RESULT_BACKEND) изолирует
    тестовый Redis от любых прод-значений из .env.
    """
    url = _redis_url(redis_server["port"])
    env = os.environ.copy()
    env.update(
        REDIS_URL=url,
        CELERY_BROKER_URL=url,
        CELERY_RESULT_BACKEND=url,
        PYTHONPATH=str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
    )

    log_file_path = Path(tempfile.mkdtemp(prefix="selti-bridge-worker-")) / "worker.log"
    worker_log = open(log_file_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "celery",
            "-A", "memory_server.celery_app",
            "worker",
            "--pool=solo",
            "--queues=memory,default",
            f"--include=tests.integration_bridge_worker",
            "--loglevel=INFO",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=worker_log,
        stderr=subprocess.STDOUT,
    )

    def _log_tail() -> str:
        worker_log.flush()
        return log_file_path.read_text(encoding="utf-8", errors="replace")[-3000:]

    client_app = Celery("selti-bridge-itest", broker=url, backend=url)
    # Продюсерская маршрутизация: без этого send_task уходит в стандартную
    # очередь "celery", которую воркер (-Q memory,default) не слушает
    client_app.conf.task_default_queue = "default"
    try:
        deadline = time.monotonic() + WORKER_READY_TIMEOUT
        while True:
            if proc.poll() is not None:
                pytest.fail(f"celery worker died at startup:\n{_log_tail()}")
            if time.monotonic() > deadline:
                pytest.fail(
                    f"celery worker not ready in {WORKER_READY_TIMEOUT}s:\n{_log_tail()}"
                )
            try:
                replies = client_app.control.inspect(timeout=1.0).ping()
                if replies and any(
                    reply.get("ok") == "pong" for reply in replies.values()
                ):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        yield {"app": client_app, "log_tail": _log_tail}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        worker_log.close()


@pytest.fixture(scope="module")
def bridge_on_test_redis(redis_server, celery_worker):
    """Notify-клиент моста главного процесса — на тестовый Redis.

    Без этого _blpop_notify ходил бы в прод-URL из настроек и деградировал
    в опрос ready(): BLPOP-путь остался бы непроверенным.
    """
    from memory_server.config import settings
    import memory_server.tools.task_bridge as bridge

    saved = (settings.redis_url, bridge._notify_client)
    settings.redis_url = _redis_url(redis_server["port"])
    bridge._notify_client = None
    try:
        yield
    finally:
        settings.redis_url, bridge._notify_client = saved


def _wait_ready(result, timeout: float = TASK_DEADLINE) -> None:
    deadline = time.monotonic() + timeout
    while not result.ready():
        if time.monotonic() > deadline:
            pytest.fail(f"task {result.id} not ready in {timeout}s")
        time.sleep(0.05)


# ── Тесты ──────────────────────────────────────────────────────────


class TestBridgeOnLiveWorker:
    # AsyncResult.__del__ на выходе процесса сыпет невосстановимым исключением
    # drainer'а celery RedisBackend (redis-py + Py3.14 shutdown) — шума не
    # боимся, к результатам теста отношения не имеет
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    def test_bridge_call_latency_result_and_notify_consumed(
        self, redis_server, celery_worker, bridge_on_test_redis
    ):
        """(а)+(б)+(в): полный цикл моста на живом воркере.

        Баг-версия (headers из kwargs диспатча): событие не публикуется,
        мост спит весь 30-секундный BLPOP-чанк и возвращается лишь по
        ready-страховке — elapsed ~30с против ≤ 2с здесь.
        """
        start = time.monotonic()
        value = run_task(
            celery_worker["app"], ECHO_TASK, timeout=TASK_DEADLINE, payload="ping"
        )
        elapsed = time.monotonic() - start

        assert elapsed <= BRIDGE_LATENCY_LIMIT, (
            f"bridge latency {elapsed:.2f}s > {BRIDGE_LATENCY_LIMIT}s — "
            f"notify-событие не будит мост (worker log:\n{celery_worker['log_tail']()})"
        )
        # (в) результат содержателен и идентифицирует задачу
        assert value["echo"] == "ping"
        assert value["task_id"]
        # (б) ключ погас: BLPOP потребил событие, списков не осталось
        assert not redis_server["client"].keys(f"{NOTIFY_KEY_PREFIX}*")

    def test_postrun_publishes_notify_key_for_bridge_wait(
        self, redis_server, celery_worker
    ):
        """Ядро прод-бага: prerun видит bridge_wait в headers реального диспатча.

        Отправка как в run_task (send_task + headers), но без потребителя:
        событие завершения должно остаться лежать в списке до TTL.
        Со старым кодом prerun ключ не публикуется вовсе.
        """
        result = celery_worker["app"].send_task(
            ECHO_TASK, kwargs={"payload": "notify"}, headers={"bridge_wait": True}
        )
        _wait_ready(result)

        key = f"{NOTIFY_KEY_PREFIX}{result.id}"
        redis_client = redis_server["client"]
        try:
            assert redis_client.exists(key) == 1, (
                "notify-ключ не опубликован: on_task_prerun не нашёл "
                f"bridge_wait (worker log:\n{celery_worker['log_tail']()})"
            )
            assert redis_client.lindex(key, 0) == "SUCCESS"
        finally:
            redis_client.delete(key)
