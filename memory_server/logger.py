"""Единый модуль логирования Argenta Team.

Формат: [ISO8601-UTC] [LEVEL] [service-name] message {"key": "value"}
POSIX-совместим: grep, jq, awk работают без проблем.
"""

from argenta_logging import setup_logging, get_logger, measure_duration

__all__ = ["setup_logging", "get_logger", "measure_duration"]
