"""JSON logging configuration for uvicorn.

Used by uvicorn.run(log_config=...) to redirect all uvicorn loggers
through structlog JSON formatter.
"""

import os
import sys
from datetime import datetime, timezone

import structlog

SERVICE_NAME = os.environ.get("SERVICE_NAME", "selti")


def _add_service(logger, method_name, event_dict):
    event_dict["service"] = SERVICE_NAME
    return event_dict


def _merge_request_id(logger, method_name, event_dict):
    try:
        from argenta_logging import request_id_var
        rid = request_id_var.get("")
        if rid:
            event_dict["request_id"] = rid
    except ImportError:
        pass
    return event_dict


def _map_level(logger, method_name, event_dict):
    level_map = {"WARNING": "WARN", "CRITICAL": "ERROR"}
    level = event_dict.get("level", "")
    event_dict["level"] = level_map.get(level, level)
    return event_dict


def _add_timestamp(logger, method_name, event_dict):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    event_dict["ts"] = ts
    return event_dict


def _rename_event_to_msg(logger, method_name, event_dict):
    if "event" in event_dict:
        event_dict["msg"] = event_dict.pop("event")
    return event_dict


_processors = [
    structlog.contextvars.merge_contextvars,
    _merge_request_id,
    structlog.stdlib.add_log_level,
    _map_level,
    _add_timestamp,
    _add_service,
]

renderer = structlog.processors.JSONRenderer()

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "structlog.stdlib.ProcessorFormatter",
            "foreign_pre_chain": _processors,
            "processors": [
                structlog.stdlib.ExtraAdder(),
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _rename_event_to_msg,
                renderer,
            ],
        },
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
    },
    "root": {"handlers": ["default"], "level": "INFO"},
}
