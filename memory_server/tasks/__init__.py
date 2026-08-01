# Celery tasks package
# Импортируем все модули с задачами для autodiscover

from memory_server.tasks import memory_tasks  # noqa: F401
from memory_server.tasks import hash_tasks  # noqa: F401
from memory_server.tasks import worker_stats  # noqa: F401
from memory_server.tasks import business_metrics  # noqa: F401
