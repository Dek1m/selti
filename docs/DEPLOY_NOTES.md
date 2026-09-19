# DEPLOY_NOTES — состояние и состав прода selti

**Сервер:** ai.atom.ui, compose: `~/app/docker-compose.yml`
**Обслуживает:** Рэй (devops). Оперативные заметки деплоя; порядок миграций — отдельно в `PHASE0_DEPLOY_ORDER.md`.

---

## Обязательный состав сервисов (проверять после каждого деплоя)

| Сервис | Контейнер | Назначение |
|---|---|---|
| `selti` | selti | MCP API :8000 |
| `celery-worker` | selti-worker | очереди `memory,batch,hash` |
| `celery-beat` | selti-beat | **периодические задачи — обязателен с 2026-09-18 (Фаза 2)** |
| `postgres` | postgres | БД memory, :5432 |
| `redis` | redis | broker + backend, :6379 |
| `qdrant` | athena-qdrant | вектора, :6333/:6334 |
| `opencode`, `nginx` | — | смежные сервисы, вне репо selti |

## celery-beat — что важно

- Тот же образ, что и воркер (`app-celery-worker:latest` на проде).
- Команда: `celery -A memory_server.celery_app beat -l INFO --schedule=/data/celerybeat-schedule`.
- **Volume `beat_schedule:/data` обязателен**: без него shelve-файл расписания живёт в контейнере и при пересоздании beat теряет метки last-run → crontab-задачи (02:00/03:00/04:00/вс 05:00–05:30 UTC) могут выполниться повторно или пропустить цикл.
- Расписание (7 записей) определено в `memory_server/celery_app.py` → `app.conf.beat_schedule`: `refresh_clusters` (02:00), `confidence_decay` (03:00), `mark_stale` (04:00), `gc_superseded` (вс 05:00), `orphans_cleanup` (вс 05:30) + легаси `worker_stats.update` (30s), `business_metrics.update` (1h).
- **Воркер запускать БЕЗ `--beat`**: beat — всегда отдельный контейнер (1 инстанс на очередь; два beat = дубли периодических задач).
- Проверка после старта: контейнер healthy, `RestartCount=0`, WAL schedule-файла обновляется (`docker exec selti-beat ls -la /data/`).

## Известные нюансы (на 2026-09-19)

1. **Легаси-задачи копятся в default-очередь.** `worker_stats.update` и `business_metrics.update` не покрыты `task_routes` в `celery_app.py` → идут в `default`, которую воркер не слушает (`-Q memory,batch,hash`). Beat честно отправляет, задачи не потребляются (рост ~120/час). Решение за Соной/Афиной: маршрут в `memory` в `task_routes` ИЛИ добавить `default` в `-Q` воркера. Lifecycle-задачи Фазы 2 маршрутизированы корректно (`memory`).
2. **Логи selti-beat пустые** (docker logs не показывает вывод beat, хотя процесс жив и тикает). Наблюдаемость beat — через Redis-очереди и schedule-файл. Причина не в infra; кандидат на разбор — Сона (logging setup / Celery 5 + Python 3.14).
3. **Healthcheck beat тавтологичен:** `grep -lqs beat /proc/[0-9]*/cmdline` матчит собственный grep — всегда exit 0. Работает де-факто как «PID 1 жив» (что и так гарантирует `restart: unless-stopped`). При случае заменить на проверку возраста schedule-файла.

## Порядок деплоя (кратко)

```
1. docker compose build celery-worker        # beat использует тот же образ
2. docker compose up -d selti celery-worker celery-beat
3. проверить: compose ps (все healthy), RestartCount=0, /data/ тикает
4. воркер без --beat; selti/worker не пересоздавать без нужды
```
