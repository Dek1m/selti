# Backup стратегия — PostgreSQL + pgvector

## Принципы

1. **WAL-based (PITR)** — основа. Без WAL-архивирования ты можешь откатиться
   только к последнему полному бэкапу. С WAL — на любой момент времени.
2. **3-2-1 правило**: 3 копии, 2 разных носителя, 1 офлайн/внешняя.
3. **Регулярные тесты восстановления** — backup, который нельзя восстановить,
   не имеет ценности.

## Компоненты

```
┌─────────────────────────────────────────────────────────────────┐
│                         Production                              │
│  ┌─────────────┐   WAL     ┌──────────────┐   push    ┌────────┐ │
│  │  PostgreSQL  │ ────────→ │  WAL-G       │ ────────→ │ S3     │ │
│  │  (athena-pg) │          │  (sidecar)    │           │ /backup│ │
│  └─────────────┘           └──────────────┘           └────────┘ │
│                                                         │
│  Раз в день: pg_dump (логический бэкап)                │
│  → тоже в S3, отдельный префикс                        │
└─────────────────────────────────────────────────────────────────┘
```

## Рекомендуемый инструмент: WAL-G

Почему WAL-G, а не pgBackRest:
- Проще конфигурация (одна env-переменная вместо конфиг-файла)
- Нативная поддержка S3/MinIO
- Стандарт де-факто для Kubernetes/Docker окружений
- Работает с pg17

## Конфигурация

### 1. На стороне PostgreSQL (postgresql.conf)

Уже настроено:
```ini
wal_level = replica
wal_compression = zstd
```

Нужно добавить:
```ini
# WAL-G архивирование
archive_mode = on
archive_command = 'wal-g wal-push %p'
archive_timeout = 60       # Форсировать архив каждые 60с (даже на тихой БД)
```

### 2. WAL-G sidecar контейнер

```yaml
wal-g:
  image: ghcr.io/wal-g/wal-g:latest
  container_name: athena-wal-g
  restart: unless-stopped
  environment:
    WALG_S3_PREFIX: s3://athena-backup/postgres/
    AWS_ENDPOINT: ${S3_ENDPOINT}
    AWS_ACCESS_KEY_ID: ${S3_ACCESS_KEY}
    AWS_SECRET_ACCESS_KEY: ${S3_SECRET_KEY}
    AWS_S3_FORCE_PATH_STYLE: "true"
    PGUSER: athena
    PGPASSWORD: ${PG_PASSWORD}
    PGHOST: postgres
    PGDATABASE: athena_memory
    WALG_COMPRESSION_METHOD: zstd
  volumes:
    - /var/run/postgresql:/var/run/postgresql
  depends_on:
    - postgres
  # Команда для периодического создания полных бэкапов (cron)
  # Используй внешний cron или Kubernetes CronJob
```

## Расписание

| Действие | Периодичность | Retention |
|----------|---------------|-----------|
| Полный бэкап (pg_dump) | Ежедневно в 03:00 | 30 дней |
| Инкрементальный (WAL) | Непрерывно | 7 дней |
| Полный физический (WAL-G backup) | Еженедельно в воскресенье 04:00 | 3 месяца |

## Скрипты

### daily_dump.sh — логический бэкап

```bash
#!/bin/bash
# pg_dump — ежедневный дамп для точечного восстановления
DATE=$(date +%Y-%m-%d)
BACKUP_DIR="/backup/dump"

pg_dump -U athena -h postgres -d athena_memory \
  --format=custom \
  --compress=zstd:3 \
  --file="${BACKUP_DIR}/athena_memory_${DATE}.dump"

# Залить в S3
wal-g put "${BACKUP_DIR}/athena_memory_${DATE}.dump" "dumps/athena_memory_${DATE}.dump"

# Оставить только последние 30 дампов локально
find ${BACKUP_DIR} -name "*.dump" -mtime +30 -delete
```

### weekly_base.sh — физический бэкап (WAL-G)

```bash
#!/bin/bash
# Полный физический бэкап (основа для PITR)
wal-g backup-push
```

## Восстановление

### Вариант A: Точечное (LSN / timestamp)
```bash
# 1. Останавливаем Postgres
# 2. Чистим PGDATA
# 3. Восстанавливаем последний полный бэкап
wal-g backup-fetch /var/lib/postgresql/data LATEST

# 4. Настраиваем recovery.conf или сигнал
touch /var/lib/postgresql/data/recovery.signal

# 5. Запускаем Postgres — он накатит WAL до последнего доступного момента
```

### Вариант B: Логический (конкретная таблица/строка)
```bash
pg_restore -U athena -h new-host -d athena_memory \
  --format=custom \
  --data-only \
  --table=memories \
  "athena_memory_2026-07-20.dump"
```

## Проверка

Раз в месяц — тест восстановления на изолированном инстансе:
```bash
docker compose -f docker-compose.restore-test.yml up --abort-on-container-exit
```
