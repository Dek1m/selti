"""Тесты для memory_server/metrics.py — метрики пула.

Проверяем:
- Все метрики импортируются
- Имеют правильные типы (Counter, Histogram, Gauge)
- Можно инкрементить / обновлять без ошибок
"""

import os
import subprocess
import sys

from prometheus_client import Counter, Gauge, Histogram

from memory_server.metrics import (
    DB_POOL_AVAILABLE,
    DB_POOL_SIZE,
    DEDUP_RATIO,
    EMBEDDING_CACHE_HIT_RATIO,
    EMBEDDING_DURATION,
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION,
    LINKER_L1C_GATE_FAILURES_TOTAL,
    MEMORY_COUNT,
    MEMORY_GROWTH_RATE,
    SEARCH_RESULTS,
    ZERO_RESULT_SEARCHES_TOTAL,
)


class TestMetricsTypes:
    """Проверка типов каждого объекта метрики."""

    def test_http_requests_total_is_counter(self):
        assert isinstance(HTTP_REQUESTS_TOTAL, Counter)

    def test_l1c_gate_failures_is_counter(self):
        """Фаза 3: fail-closed каскады гейта L1c — Counter без лейблов."""
        assert isinstance(LINKER_L1C_GATE_FAILURES_TOTAL, Counter)
        assert LINKER_L1C_GATE_FAILURES_TOTAL._labelnames == ()

    def test_http_request_duration_is_histogram(self):
        assert isinstance(HTTP_REQUEST_DURATION, Histogram)

    def test_db_pool_size_is_gauge(self):
        assert isinstance(DB_POOL_SIZE, Gauge)

    def test_db_pool_available_is_gauge(self):
        assert isinstance(DB_POOL_AVAILABLE, Gauge)

    def test_embedding_duration_is_histogram(self):
        assert isinstance(EMBEDDING_DURATION, Histogram)

    def test_search_results_is_histogram(self):
        assert isinstance(SEARCH_RESULTS, Histogram)

    def test_zero_result_searches_is_counter(self):
        assert isinstance(ZERO_RESULT_SEARCHES_TOTAL, Counter)

    def test_memory_count_is_gauge(self):
        assert isinstance(MEMORY_COUNT, Gauge)

    def test_dedup_ratio_is_gauge(self):
        assert isinstance(DEDUP_RATIO, Gauge)

    def test_memory_growth_rate_is_gauge(self):
        assert isinstance(MEMORY_GROWTH_RATE, Gauge)

    def test_embedding_cache_hit_ratio_is_gauge(self):
        assert isinstance(EMBEDDING_CACHE_HIT_RATIO, Gauge)


class TestMetricsLabels:
    """Проверка, что лейблы заданы корректно."""

    def test_http_requests_total_has_method_endpoint_status_labels(self):
        labels = HTTP_REQUESTS_TOTAL._labelnames
        assert "method" in labels
        assert "endpoint" in labels
        assert "status" in labels

    def test_http_request_duration_has_method_endpoint_labels(self):
        labels = HTTP_REQUEST_DURATION._labelnames
        assert "method" in labels
        assert "endpoint" in labels

    def test_memory_count_has_namespace_label(self):
        labels = MEMORY_COUNT._labelnames
        assert "namespace" in labels

    def test_search_results_has_tool_label(self):
        labels = SEARCH_RESULTS._labelnames
        assert "tool" in labels

    def test_zero_result_searches_has_namespace_label(self):
        labels = ZERO_RESULT_SEARCHES_TOTAL._labelnames
        assert "namespace" in labels

    def test_search_results_small_buckets(self):
        """Малые бакеты (Фаза 3.3): различимы пустая выдача, 1-2 и 3-4 результата."""
        assert list(SEARCH_RESULTS._upper_bounds) == [0.0, 1.0, 3.0, 5.0, 10.0, 20.0, float("Inf")]

    def test_dedup_ratio_has_namespace_label(self):
        labels = DEDUP_RATIO._labelnames
        assert "namespace" in labels

    def test_memory_growth_rate_has_namespace_label(self):
        labels = MEMORY_GROWTH_RATE._labelnames
        assert "namespace" in labels


class TestMetricsOperations:
    """Проверка, что метрики можно инкрементить / обновлять без ошибок."""

    def test_counter_increment(self):
        HTTP_REQUESTS_TOTAL.labels(method="GET", endpoint="/test", status="200").inc()
        HTTP_REQUESTS_TOTAL.labels(method="GET", endpoint="/test", status="200").inc(2)
        value = HTTP_REQUESTS_TOTAL.labels(
            method="GET", endpoint="/test", status="200"
        )._value.get()
        assert value == 3.0

    def test_histogram_observe(self):
        HTTP_REQUEST_DURATION.labels(method="POST", endpoint="/api").observe(0.1)
        EMBEDDING_DURATION.observe(0.05)
        SEARCH_RESULTS.labels(tool="memory_search").observe(5)

    def test_gauge_set_and_clear(self):
        DB_POOL_SIZE.set(10)
        assert DB_POOL_SIZE._value.get() == 10.0
        DB_POOL_SIZE.set(0)

        DB_POOL_AVAILABLE.set(8)
        assert DB_POOL_AVAILABLE._value.get() == 8.0

        MEMORY_COUNT.labels(namespace="default").set(42)
        MEMORY_COUNT.labels(namespace="custom").set(7)

    def test_gauge_dec_inc(self):
        DB_POOL_SIZE.set(5)
        DB_POOL_SIZE.inc(2)
        assert DB_POOL_SIZE._value.get() == 7.0
        DB_POOL_SIZE.dec(3)
        assert DB_POOL_SIZE._value.get() == 4.0

    def test_multiple_counter_labels(self):
        """Разные комбинации лейблов не должны пересекаться."""
        HTTP_REQUESTS_TOTAL.labels(method="GET", endpoint="/a", status="200").inc()
        HTTP_REQUESTS_TOTAL.labels(method="POST", endpoint="/b", status="500").inc(3)

        get_val = HTTP_REQUESTS_TOTAL.labels(
            method="GET", endpoint="/a", status="200"
        )._value.get()
        post_val = HTTP_REQUESTS_TOTAL.labels(
            method="POST", endpoint="/b", status="500"
        )._value.get()

        assert get_val == 1.0
        assert post_val == 3.0

    def test_dedup_ratio_gauge_set(self):
        DEDUP_RATIO.labels(namespace="default").set(0.75)
        assert DEDUP_RATIO.labels(namespace="default")._value.get() == 0.75

    def test_memory_growth_rate_gauge_set(self):
        MEMORY_GROWTH_RATE.labels(namespace="code_knowledge").set(12.5)
        assert MEMORY_GROWTH_RATE.labels(namespace="code_knowledge")._value.get() == 12.5

    def test_embedding_cache_hit_ratio_gauge_set(self):
        EMBEDDING_CACHE_HIT_RATIO.set(0.85)
        assert EMBEDDING_CACHE_HIT_RATIO._value.get() == 0.85


class TestNoPatchImport:
    """Регрессия (приёмка Фазы 1): metrics.py падал TypeError при прямом
    импорте — multiprocess_mode передавался в Counter (валиден только для
    Gauge). Костыль-мок из conftest удалён, честность проверяем импортом
    в чистом subprocess: никаких патчей prometheus_client.
    """

    def test_module_imports_without_patches(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-c", "import memory_server.metrics"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        assert result.returncode == 0, (
            f"прямой импорт memory_server.metrics упал:\n{result.stderr}"
        )
