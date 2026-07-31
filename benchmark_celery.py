"""Benchmark Celery task latency for selti migration (Phase 5).

Меряет:
1. Время импорта модулей (cold start)
2. AsyncBridge overhead (run_async)
3. Task execution latency (task_always_eager=True)
4. Serialization overhead (Pydantic → JSON → dict)
5. P50/P95/P99 latency для memory_ops и hash_ops

Цель (plan v3):
- memory_ops P95 < 2s
- hash_ops P95 < 1s

Запуск: .venv/bin/python benchmark_celery.py
"""

import asyncio
import statistics
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


def benchmark_imports():
    """Замер времени импорта модулей (cold start)."""
    print("=" * 70)
    print("1. MODULE IMPORT BENCHMARK (cold start)")
    print("=" * 70)

    modules = [
        ("memory_server.config", "Config + pydantic-settings"),
        ("memory_server.tasks.async_bridge", "AsyncBridge (run_async)"),
        ("memory_server.tasks.base", "SeltiTask base class"),
        ("memory_server.tasks.errors", "Custom exceptions"),
        ("memory_server.tasks.connections", "Worker singletons (asyncpg+qdrant)"),
        ("memory_server.tasks.memory_tasks", "15 memory tasks"),
        ("memory_server.tasks.hash_tasks", "4 hash tasks"),
        ("memory_server.celery_app", "Celery app instance"),
    ]

    total = 0
    for mod, desc in modules:
        start = time.perf_counter()
        try:
            __import__(mod)
            duration = time.perf_counter() - start
            print(f"  OK  {mod}: {duration*1000:7.2f}ms — {desc}")
        except Exception as e:
            duration = time.perf_counter() - start
            print(f"  FAIL {mod}: {duration*1000:7.2f}ms — {type(e).__name__}: {e}")
        total += duration

    print(f"\n  Total cold start: {total*1000:.2f}ms")
    return total


def benchmark_async_bridge():
    """Замер overhead AsyncBridge (run_async)."""
    print("\n" + "=" * 70)
    print("2. ASYNC BRIDGE OVERHEAD (run_async)")
    print("=" * 70)

    from memory_server.tasks.async_bridge import run_async

    async def noop():
        pass

    async def lightweight():
        return 42

    async def with_await():
        await asyncio.sleep(0)
        return "done"

    # Warmup
    for _ in range(10):
        run_async(noop)

    # Benchmark: noop (pure overhead)
    iterations = 1000
    durations = []
    start_total = time.perf_counter()
    for _ in range(iterations):
        start = time.perf_counter()
        run_async(noop)
        durations.append(time.perf_counter() - start)
    total = time.perf_counter() - start_total

    p50 = statistics.median(durations)
    p95 = sorted(durations)[int(len(durations) * 0.95)]
    p99 = sorted(durations)[int(len(durations) * 0.99)]

    print(f"  noop (pure overhead) × {iterations}:")
    print(f"    avg:  {statistics.mean(durations)*1000:.4f}ms")
    print(f"    P50:  {p50*1000:.4f}ms")
    print(f"    P95:  {p95*1000:.4f}ms")
    print(f"    P99:  {p99*1000:.4f}ms")
    print(f"    total: {total*1000:.2f}ms for {iterations} calls")

    # Benchmark: with_await
    durations2 = []
    for _ in range(iterations):
        start = time.perf_counter()
        run_async(with_await)
        durations2.append(time.perf_counter() - start)

    p50_2 = statistics.median(durations2)
    p95_2 = sorted(durations2)[int(len(durations2) * 0.95)]

    print(f"\n  with_await (event loop + coroutine) × {iterations}:")
    print(f"    avg:  {statistics.mean(durations2)*1000:.4f}ms")
    print(f"    P50:  {p50_2*1000:.4f}ms")
    print(f"    P95:  {p95_2*1000:.4f}ms")

    return p50, p95


def benchmark_task_execution():
    """Замер latency выполнения задач через Celery (task_always_eager)."""
    print("\n" + "=" * 70)
    print("3. TASK EXECUTION LATENCY (task_always_eager=True)")
    print("=" * 70)

    # Setup Celery in eager mode
    from memory_server.celery_app import app
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True

    # Mock connections to avoid real DB/Qdrant
    mock_pool = MagicMock()
    mock_qdrant = MagicMock()
    mock_embedding = MagicMock()

    mock_svc = MagicMock()
    record = MagicMock()
    record.model_dump.return_value = {
        "id": "mem-1", "user_id": "u1", "content": "test",
        "metadata": {}, "namespace": "default", "importance": 3,
        "created_at": "2026-07-31T12:00:00Z", "updated_at": "2026-07-31T12:00:00Z",
    }
    action = MagicMock()
    action.value = "insert"
    mock_svc.store = AsyncMock(return_value=(record, action))
    mock_svc.get = AsyncMock(return_value=record)
    mock_svc.update = AsyncMock(return_value=record)
    mock_svc.delete = AsyncMock(return_value=True)
    search_result = MagicMock()
    search_result.model_dump.return_value = {
        "id": "sr-1", "content": "found", "metadata": {},
        "importance": 3, "score": 0.95,
    }
    mock_svc.search = AsyncMock(return_value=[search_result])
    list_result = MagicMock()
    list_result.items = [record]
    list_result.total = 1
    mock_svc.list = AsyncMock(return_value=list_result)
    mock_svc.recent = AsyncMock(return_value=[record])
    stats_item = MagicMock()
    stats_item.model_dump.return_value = {"namespace": "default", "count": 5, "last_updated": None}
    mock_svc.get_stats = AsyncMock(return_value=[stats_item])
    graph_stats = MagicMock()
    graph_stats.model_dump.return_value = {
        "total_granules": 10, "total_relations": 5, "linked_granules": 8,
        "orphans": 2, "avg_connections": 0.5, "by_namespace": {}, "by_link_type": {},
    }
    mock_svc.get_graph_stats = AsyncMock(return_value=graph_stats)
    traverse_result = MagicMock()
    traverse_result.nodes = [{"id": "n1"}]
    traverse_result.edges = []
    mock_svc.traverse = AsyncMock(return_value=traverse_result)
    mock_svc.archive = AsyncMock(return_value=True)
    mock_svc.forget = AsyncMock(return_value=3)
    mock_svc.ns_repo = MagicMock()
    ns_record = MagicMock()
    ns_record.id = "ns-1"
    mock_svc.ns_repo.get_or_create = AsyncMock(return_value=ns_record)
    mock_svc.repository = MagicMock()
    mock_svc.repository.insert_batch = AsyncMock(return_value=["mem-1"])
    mock_svc.repository.sync_links_batch = AsyncMock()
    mock_svc.repository.get_relations_by_source = AsyncMock(return_value=[])
    mock_svc.repository.get_relations_by_target = AsyncMock(return_value=[])
    mock_svc.config = MagicMock()
    mock_svc.config.dedup_enabled = False
    mock_svc.add_relation = AsyncMock(return_value="rel-1")
    mock_svc.delete_relation = AsyncMock(return_value=True)

    mock_emb = MagicMock()
    mock_emb.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    mock_emb.embed_many = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

    hash_repo = MagicMock()
    hash_repo.upsert = AsyncMock(return_value={
        "id": "h1", "source_type": "file", "source_id": "f1",
        "content_hash": "a" * 64, "created_at": "2026-07-31T12:00:00Z",
        "updated_at": "2026-07-31T12:00:00Z",
    })
    hash_repo.get = AsyncMock(return_value={
        "id": "h1", "source_type": "file", "source_id": "f1",
        "content_hash": "a" * 64, "created_at": "2026-07-31T12:00:00Z",
        "updated_at": "2026-07-31T12:00:00Z",
    })
    hash_repo.delete = AsyncMock(return_value="h1")
    hash_repo.list = AsyncMock(return_value=[])

    iterations = 200

    # ── Memory Ops ──
    memory_tasks = [
        ("memory_store", lambda: __import__("memory_server.tasks.memory_tasks", fromlist=["store_memory"]).store_memory(
            content="benchmark test content for latency measurement",
            user_id="benchmark-user",
            metadata={"benchmark": True},
            namespace="default",
            importance=3,
        )),
        ("memory_get", lambda: __import__("memory_server.tasks.memory_tasks", fromlist=["get_memory"]).get_memory(
            memory_id="mem-1",
        )),
        ("memory_search", lambda: __import__("memory_server.tasks.memory_tasks", fromlist=["search_memories"]).search_memories(
            query="benchmark query",
            user_id="benchmark-user",
            limit=10,
            threshold=0.7,
        )),
        ("memory_update", lambda: __import__("memory_server.tasks.memory_tasks", fromlist=["update_memory"]).update_memory(
            memory_id="mem-1",
            content="updated content",
            importance=4,
        )),
        ("memory_delete", lambda: __import__("memory_server.tasks.memory_tasks", fromlist=["delete_memory"]).delete_memory(
            memory_id="mem-1",
        )),
    ]

    print("\n  MEMORY OPS:")
    memory_results = {}
    for name, task_fn in memory_tasks:
        durations = []
        with patch("memory_server.tasks.memory_tasks._get_service", return_value=mock_svc), \
             patch("memory_server.tasks.memory_tasks.get_embedding", return_value=mock_emb):
            for _ in range(iterations):
                start = time.perf_counter()
                task_fn()
                durations.append(time.perf_counter() - start)

        p50 = statistics.median(durations)
        p95 = sorted(durations)[int(len(durations) * 0.95)]
        p99 = sorted(durations)[int(len(durations) * 0.99)]
        memory_results[name] = {"p50": p50, "p95": p95, "p99": p99}

        target = "< 2s"
        passed = "PASS" if p95 < 2.0 else "FAIL"
        print(f"    {name:20s} P50={p50*1000:7.2f}ms  P95={p95*1000:7.2f}ms  P99={p99*1000:7.2f}ms  [{passed}] (target {target})")

    # ── Hash Ops ──
    hash_tasks = [
        ("hash_upsert", lambda: __import__("memory_server.tasks.hash_tasks", fromlist=["upsert_hash"]).upsert_hash(
            source_type="file",
            source_id="benchmark-file",
            content_hash="a" * 64,
            size_bytes=1024,
        )),
        ("hash_get", lambda: __import__("memory_server.tasks.hash_tasks", fromlist=["get_hash"]).get_hash(
            source_type="file",
            source_id="benchmark-file",
        )),
        ("hash_delete", lambda: __import__("memory_server.tasks.hash_tasks", fromlist=["delete_hash"]).delete_hash(
            source_type="file",
            source_id="benchmark-file",
        )),
        ("hash_list", lambda: __import__("memory_server.tasks.hash_tasks", fromlist=["list_hashes"]).list_hashes(
            source_type="file",
            limit=10,
        )),
    ]

    print("\n  HASH OPS:")
    hash_results = {}
    for name, task_fn in hash_tasks:
        durations = []
        with patch("memory_server.tasks.hash_tasks._get_hash_repo", return_value=hash_repo):
            for _ in range(iterations):
                start = time.perf_counter()
                task_fn()
                durations.append(time.perf_counter() - start)

        p50 = statistics.median(durations)
        p95 = sorted(durations)[int(len(durations) * 0.95)]
        p99 = sorted(durations)[int(len(durations) * 0.99)]
        hash_results[name] = {"p50": p50, "p95": p95, "p99": p99}

        target = "< 1s"
        passed = "PASS" if p95 < 1.0 else "FAIL"
        print(f"    {name:20s} P50={p50*1000:7.2f}ms  P95={p95*1000:7.2f}ms  P99={p99*1000:7.2f}ms  [{passed}] (target {target})")

    return memory_results, hash_results


def benchmark_serialization():
    """Замер overhead сериализации Pydantic → JSON."""
    print("\n" + "=" * 70)
    print("4. SERIALIZATION OVERHEAD (Pydantic model_dump)")
    print("=" * 70)

    from pydantic import BaseModel
    from typing import Optional
    from datetime import datetime

    class FakeRecord(BaseModel):
        id: str
        user_id: str
        content: str
        metadata: dict
        namespace: str
        importance: int
        created_at: datetime
        updated_at: datetime
        embedding: Optional[list[float]] = None

    record = FakeRecord(
        id="mem-benchmark-001",
        user_id="benchmark-user",
        content="This is a benchmark content string with some reasonable length to simulate real data that would be stored in the memory system.",
        metadata={"source": "benchmark", "tags": ["test", "latency"], "nested": {"key": "value"}},
        namespace="code_knowledge",
        importance=4,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        embedding=[0.1] * 4096,  # Realistic embedding dimension
    )

    iterations = 5000

    # model_dump(mode="json") — what tasks actually use
    durations_json = []
    for _ in range(iterations):
        start = time.perf_counter()
        record.model_dump(mode="json")
        durations_json.append(time.perf_counter() - start)

    p50 = statistics.median(durations_json)
    p95 = sorted(durations_json)[int(len(durations_json) * 0.95)]

    print(f"  model_dump(mode='json') × {iterations}:")
    print(f"    avg:  {statistics.mean(durations_json)*1000:.4f}ms")
    print(f"    P50:  {p50*1000:.4f}ms")
    print(f"    P95:  {p95*1000:.4f}ms")

    # model_dump(mode="python") — for comparison
    durations_py = []
    for _ in range(iterations):
        start = time.perf_counter()
        record.model_dump(mode="python")
        durations_py.append(time.perf_counter() - start)

    p50_py = statistics.median(durations_py)
    p95_py = sorted(durations_py)[int(len(durations_py) * 0.95)]

    print(f"\n  model_dump(mode='python') × {iterations} (baseline):")
    print(f"    avg:  {statistics.mean(durations_py)*1000:.4f}ms")
    print(f"    P50:  {p50_py*1000:.4f}ms")
    print(f"    P95:  {p95_py*1000:.4f}ms")

    overhead = ((p50 - p50_py) / p50_py * 100) if p50_py > 0 else 0
    print(f"\n  JSON mode overhead vs Python: {overhead:+.1f}%")


def benchmark_celery_config():
    """Проверка конфигурации Celery."""
    print("\n" + "=" * 70)
    print("5. CELERY CONFIGURATION CHECK")
    print("=" * 70)

    from memory_server.celery_app import app

    checks = {
        "broker_url": app.conf.broker_url,
        "result_backend": app.conf.result_backend,
        "task_serializer": app.conf.task_serializer,
        "result_serializer": app.conf.result_serializer,
        "task_acks_late": app.conf.task_acks_late,
        "task_reject_on_worker_lost": app.conf.task_reject_on_worker_lost,
        "worker_prefetch_multiplier": app.conf.worker_prefetch_multiplier,
        "worker_max_tasks_per_child": app.conf.worker_max_tasks_per_child,
        "worker_max_memory_per_child": app.conf.worker_max_memory_per_child,
        "worker_soft_shutdown_timeout": app.conf.worker_soft_shutdown_timeout,
        "task_soft_time_limit": app.conf.task_soft_time_limit,
        "task_time_limit": app.conf.task_time_limit,
        "result_expires": app.conf.result_expires,
        "worker_concurrency": app.conf.worker_concurrency,
        "task_queues": [q.name for q in app.conf.task_queues],
        "task_routes": app.conf.task_routes,
    }

    for key, val in checks.items():
        print(f"  {key:40s} = {val}")

    # Check tasks registered
    print(f"\n  Autodiscovered tasks:")
    inspector = app.control.inspect()
    try:
        registered = inspector.registered()
        if registered:
            for worker, tasks in registered.items():
                print(f"    Worker '{worker}': {len(tasks)} tasks")
                for t in sorted(tasks):
                    print(f"      - {t}")
        else:
            print("    (no workers running — eager mode)")
            # List from config
            task_list = app.tasks.keys()
            selti_tasks = [t for t in task_list if t.startswith("memory_server.")]
            print(f"    Registered in app: {len(selti_tasks)} tasks")
            for t in sorted(selti_tasks):
                print(f"      - {t}")
    except Exception as e:
        print(f"    (inspect error: {e})")


def main():
    print("╔" + "═" * 68 + "╗")
    print("║  CELERY MIGRATION BENCHMARK — Phase 5                                ║")
    print("║  Project: selti — Python MCP semantic memory server                  ║")
    print("║  Target: memory_ops P95 < 2s, hash_ops P95 < 1s                    ║")
    print("╚" + "═" * 68 + "╝")

    start = time.perf_counter()

    benchmark_imports()
    benchmark_async_bridge()
    memory_results, hash_results = benchmark_task_execution()
    benchmark_serialization()
    benchmark_celery_config()

    total = time.perf_counter() - start

    # ── Summary ──
    print("\n" + "=" * 70)
    print("6. SUMMARY")
    print("=" * 70)

    # Check targets
    memory_p95s = [v["p95"] for v in memory_results.values()]
    hash_p95s = [v["p95"] for v in hash_results.values()]
    max_memory_p95 = max(memory_p95s) if memory_p95s else 0
    max_hash_p95 = max(hash_p95s) if hash_p95s else 0

    mem_target = "PASS" if max_memory_p95 < 2.0 else "FAIL"
    hash_target = "PASS" if max_hash_p95 < 1.0 else "FAIL"

    print(f"  Memory ops worst P95: {max_memory_p95*1000:.2f}ms  [{mem_target}] (target < 2000ms)")
    print(f"  Hash ops worst P95:   {max_hash_p95*1000:.2f}ms  [{hash_target}] (target < 1000ms)")
    print(f"  Total benchmark time: {total:.2f}s")

    overall = "PASS" if (mem_target == "PASS" and hash_target == "PASS") else "FAIL"
    print(f"\n  OVERALL: [{overall}]")

    if overall == "FAIL":
        print("\n  RECOMMENDATIONS:")
        if mem_target == "FAIL":
            worst_mem = max(memory_results.items(), key=lambda x: x[1]["p95"])
            print(f"    - Optimize {worst_mem[0]} (P95={worst_mem[1]['p95']*1000:.2f}ms)")
        if hash_target == "FAIL":
            worst_hash = max(hash_results.items(), key=lambda x: x[1]["p95"])
            print(f"    - Optimize {worst_hash[0]} (P95={worst_hash[1]['p95']*1000:.2f}ms)")


if __name__ == "__main__":
    main()
