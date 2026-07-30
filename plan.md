# План миграции pgvector → Qdrant

**Дата:** 2026-07-30
**Автор:** Момо (Planner)
**Статус:** Готов к утверждению

---

## Краткое описание

Миграция векторного хранилища athena-memory (selti) с PostgreSQL + pgvector на Qdrant. Qdrant становится единственным векторным хранилищем. PostgreSQL остаётся для метаданных и графа знаний.

**Текущее состояние:**
- 184 записи от 10 user_id (74 akame, 14 athena, ~50 knowledge-analyzer, и др.)
- Эмбеддинги: 4096-dim (Qwen3-embedding-8b)
- Проблема: pgvector не может создать HNSW индекс для 4096-dim (лимит 2000), sequential scan

**Целевое состояние:**
- Qdrant: HNSW индекс, 4096-dim, cosine distance, on-disk
- PostgreSQL: метаданные + граф знаний (без embedding колонки)
- Docker: замена pgvector/pgvector:pg18 → postgres:18

---

## Цели и анти-цели

### Цели
1. **Производительность**: HNSW индекс в Qdrant вместо sequential scan
2. **Масштабируемость**: Qdrant масштабируется от 1K до 100M+ записей
3. **Простота**: убрать pgvector зависимость, упростить Docker образ
4. **Нулевой даунтайм**: dual-write миграция без остановки сервиса

### Анти-цели
- ❌ Не трогаем relations (граф знаний) — остаётся в PostgreSQL
- ❌ Не меняем embedding модель (Qwen3-embedding-8b, 4096-dim)
- ❌ Не трогаем Redis кэш
- ❌ Не меняем MCP API (инструменты остаются те же)

---

## Инфраструктура

### Сервер: ai.atom.ui
- ОС: CentOS Stream 10
- Docker: 29.5.2, Compose v5.1.4
- RAM: 3.5G (2.1G занято)
- Disk: 41G (19G занято)
- SSH: svc_athene_ai@ai.atom.ui

### Контейнеры (текущие)
| Контейнер | Образ | Порт | Том |
|-----------|-------|------|-----|
| postgres | pgvector/pgvector:pg18 | 5432 | /opt/data/postgres |
| redis | redis:8-alpine | 6379 | — |
| athena-memory | memory-server:latest | 8000 | — |
| opencode | opencode | 3000 | — |
| gera | — | — | — |

### Контейнеры (после миграции)
| Контейнер | Образ | Порт | Том |
|-----------|-------|------|-----|
| postgres | postgres:18 | 5432 | /opt/data/postgres |
| redis | redis:8-alpine | 6379 | — |
| **qdrant** | **qdrant/qdrant:latest** | **6333, 6334** | **/opt/data/qdrant** |
| athena-memory | memory-server:latest | 8000 | — |
| opencode | opencode | 3000 | — |
| gera | — | — | — |

---

## Фазы реализации

---

### Фаза 0: Подготовка (День 1)

**Цель:** Убедиться, что все готовы к миграции

**Задачи:**

#### 0.1 Бэкап PostgreSQL
- **Исполнитель:** Рэй
- **Действия:**
  1. SSH на ai.atom.ui: `ssh svc_athene_ai@ai.atom.ui`
  2. Остановить athena-memory: `docker stop athena-memory`
  3. Бэкап: `docker exec athena-pg pg_dump -U athena athene_memory > /opt/data/backup_pre_migration_$(date +%Y%m%d).sql`
  4. Проверить размер бэкапа: `ls -lh /opt/data/backup_pre_migration_*.sql`
  5. Запустить athena-memory: `docker start athena-memory`
- **Проверка:** Бэкап существует и его можно восстановить
- **Откат:** Бэкап уже сделан
- **Время:** 10 минут

#### 0.2 Проверка текущего состояния
- **Исполнитель:** Катерина
- **Действия:**
  1. Запустить тесты: `cd /home/opencode/projects/selti && python -m pytest tests/ -v`
  2. Проверить количество записей: `SELECT count(*) FROM memories WHERE embedding IS NOT NULL AND is_archived = false;`
  3. Проверить количество pending миграции: `SELECT * FROM verify_qdrant_migration();` (если таблица существует)
- **Проверка:** Все тесты проходят, количество записей известно
- **Время:** 15 минут

#### 0.3 Коммит текущего состояния
- **Исполнитель:** Момо
- **Действия:**
  1. `cd /home/opencode/projects/selti`
  2. `git status` — убедиться что нет незакоммиченных изменений
  3. Если есть — закоммитить или сделать stash
  4. `git log --oneline -5` — запомнить текущий коммит для отката
- **Проверка:** Чистый git status
- **Время:** 5 минут

---

### Фаза 1: Инфраструктура — Qdrant (День 1)

**Цель:** Запустить Qdrant контейнер

**Задачи:**

#### 1.1 Обновить docker-compose.yml
- **Исполнитель:** Рэй
- **Файл:** `docker-compose.yml`
- **Действия:**
  1. Добавить сервис qdrant:
  ```yaml
  qdrant:
    image: qdrant/qdrant:latest
    container_name: athena-qdrant
    restart: unless-stopped
    ports:
      - "127.0.0.1:6333:6333"  # HTTP API
      - "127.0.0.1:6334:6334"  # gRPC
    volumes:
      - qdrant-data:/qdrant/storage
    environment:
      QDRANT__SERVICE__GRPC_PORT: "6334"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/healthz"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    networks:
      - athena-net
    deploy:
      resources:
        limits:
          memory: 1G
  ```
  2. Добавить volume:
  ```yaml
  volumes:
    pgdata:
      driver: local
    redis-data:
      driver: local
    qdrant-data:
      driver: local
  ```
  3. Добавить depends_on в memory-server:
  ```yaml
  depends_on:
    postgres:
      condition: service_healthy
    redis:
      condition: service_healthy
    qdrant:
      condition: service_healthy
  ```
- **Проверка:** `docker compose config` проходит без ошибок
- **Откат:** Убрать qdrant сервис из docker-compose.yml
- **Время:** 15 минут

#### 1.2 Развернуть Qdrant на ai.atom.ui
- **Исполнитель:** Рэй
- **Действия:**
  1. SSH на сервер: `ssh svc_athene_ai@ai.atom.ui`
  2. Перейти в ~/app: `cd ~/app`
  3. Скопировать обновлённый docker-compose.yml
  4. Запустить Qdrant: `docker compose up -d qdrant`
  5. Проверить здоровье: `docker compose ps qdrant`
  6. Проверить API: `curl http://127.0.0.1:6333/healthz`
  7. Проверить коллекции: `curl http://127.0.0.1:6333/collections`
- **Проверка:** Qdrant отвечает на healthz, collections — пустой список
- **Откат:** `docker compose down qdrant && docker volume rm qdrant-data`
- **Время:** 10 минут

#### 1.3 Создать коллекцию memories
- **Исполнитель:** Нора
- **Действия:**
  1. Скопировать `migrations/setup_qdrant_collection.py` на сервер
  2. Установить зависимости: `pip install qdrant-client python-dotenv`
  3. Запустить: `python setup_qdrant_collection.py`
  4. Проверить: `python setup_qdrant_collection.py --info`
- **Проверка:** Коллекция memories создана, 0 points, 3 payload индекса (namespace, user_id, importance)
- **Откат:** `python setup_qdrant_collection.py --recreate` или удалить коллекцию через API
- **Время:** 10 минут

**Итого Фаза 1:** ~40 минут

---

### Фаза 2: Конфигурация (День 1)

**Цель:** Обновить конфигурацию для работы с Qdrant

**Задачи:**

#### 2.1 Обновить .env на сервере
- **Исполнитель:** Рэй
- **Файл:** `.env` на ai.atom.ui (~/app/.env)
- **Действия:**
  1. Добавить переменные:
  ```
  QDRANT_URL=http://qdrant:6333
  QDRANT_COLLECTION=memories
  QDRANT_ENABLED=true
  ```
  2. Проверить что DATABASE_URL указывает на athena-pg
- **Проверка:** `docker compose exec athena-memory env | grep QDRANT`
- **Время:** 5 минут

#### 2.2 Обновить requirements.txt
- **Исполнитель:** Сона
- **Файл:** `requirements.txt`
- **Действия:**
  1. Добавить: `qdrant-client>=1.12.0`
  2. Оставить `pgvector>=0.3.0` (пока нужен для миграции)
- **Проверка:** `pip install -r requirements.txt` проходит
- **Время:** 5 минут

#### 2.3 Обновить Dockerfile
- **Исполнитель:** Рэй
- **Файл:** `Dockerfile`
- **Действия:**
  1. Убедиться что qdrant-client установится через requirements.txt
  2. Проверить что нет конфликтов зависимостей
- **Проверка:** `docker build -t memory-server:test .` проходит
- **Время:** 10 минут

**Итого Фаза 2:** ~20 минут

---

### Фаза 3: Qdrant Client Layer (День 1-2)

**Цель:** Интегрировать QdrantClient в сервер

**Задачи:**

#### 3.1 Обновить server.py — интеграция QdrantClient
- **Исполнитель:** Сона
- **Файл:** `memory_server/server.py`
- **Действия:**
  1. Добавить импорт: `from qdrant_client import QdrantClient`
  2. В lifespan создать QdrantClient:
  ```python
  from qdrant_client import QdrantClient
  
  qdrant_client = None
  if settings.qdrant_enabled:
      qdrant_client = QdrantClient(url=settings.qdrant_url, timeout=30)
      logger.info("Qdrant connected: url=%s", settings.qdrant_url)
  ```
  3. Передать qdrant_client в repository:
  ```python
  repository = MemoryRepository(
      pool=pool,
      qdrant=qdrant_client,
      qdrant_collection=settings.qdrant_collection,
  )
  ```
  4. В finally закрыть:
  ```python
  if qdrant_client:
      qdrant_client.close()
  ```
- **Проверка:** Сервер стартует, в логах видно "Qdrant connected"
- **Откат:** Убрать QdrantClient, вернуть старый repository
- **Время:** 20 минут

#### 3.2 Обновить pool.py — убрать pgvector codec
- **Исполнитель:** Сона
- **Файл:** `memory_server/db/pool.py`
- **Действия:**
  1. Убрать импорт: `from pgvector.asyncpg import register_vector`
  2. Убрать вызов: `await register_vector(conn)` из init_conn
  3. Оставить только JSONB codec и statement_timeout
- **Проверка:** Пул создаётся, соединения работают
- **Откат:** Вернуть register_vector
- **Время:** 5 минут

#### 3.3 Обновить queries.py — убрать embedding из SQL
- **Исполнитель:** Сона
- **Файл:** `memory_server/db/queries.py`
- **Действия:**
  1. Обновить INSERT_MEMORY:
  ```sql
  INSERT INTO memories (user_id, content, metadata, namespace, namespace_id, content_hash, importance)
  VALUES ($1, $2, $3::jsonb, $4, $5::uuid, $6, $7)
  RETURNING id
  ```
  2. Обновить INSERT_MEMORY_BATCH:
  ```sql
  INSERT INTO memories (user_id, content, metadata, namespace, namespace_id, content_hash, importance)
  SELECT
      unnest($1::text[]),
      unnest($2::text[]),
      unnest($3::jsonb[]),
      unnest($4::text[]),
      unnest($5::uuid[]),
      unnest($6::text[]),
      unnest($7::int[])
  RETURNING id
  ```
  3. Убрать SEARCH_MEMORIES (sequential scan с pgvector) — заменит Qdrant search
  4. Обновить UPDATE_MEMORY:
  ```sql
  UPDATE memories
  SET content = COALESCE($2, content),
      metadata = COALESCE($3::jsonb, metadata),
      importance = COALESCE($4, importance),
      updated_at = now()
  WHERE id = $1
  RETURNING id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
  ```
  5. Оставить все остальные запросы без изменений (relations, graph, traverse)
- **Проверка:** SQL запросы корректны, нет синтаксических ошибок
- **Откат:** git checkout queries.py
- **Время:** 15 минут

**Итого Фаза 3:** ~40 минут

---

### Фаза 4: Dual-Write Repository (День 2)

**Цель:** Заменить repository.py на repository_qdrant.py

**Задачи:**

#### 4.1 Заменить repository.py
- **Исполнитель:** Сона
- **Файл:** `memory_server/memory/repository.py`
- **Действия:**
  1. Сделать бэкап: `cp repository.py repository_pgvector.py`
  2. Скопировать repository_qdrant.py → repository.py
  3. Проверить что импорты корректны
  4. Убедиться что dual-write логика работает:
     - Если Qdrant доступен: вектор → Qdrant, метаданные → PG
     - Если Qdrant недоступен: fallback на старый паттерн
- **Проверка:** Модуль импортируется без ошибок
- **Откат:** `cp repository_pgvector.py repository.py`
- **Время:** 10 минут

#### 4.2 Обновить service.py — убрать embedding из repository calls
- **Исполнитель:** Сона
- **Файл:** `memory_server/memory/service.py`
- **Действия:**
  1. В методе store: repository.insert() принимает embedding — это ок (repository_qdrant сам решает куда вектор)
  2. В методе update: repository.update() принимает embedding — ок
  3. Проверить что DedupEngine работает корректно
- **Проверка:** Все методы service вызываются без ошибок
- **Время:** 10 минут

#### 4.3 Обновить server.py lifespan
- **Исполнитель:** Сона
- **Файл:** `memory_server/server.py`
- **Действия:**
  1. Убрать register_vector из pool init (уже сделано в 3.2)
  2. Проверить что repository получает qdrant_client
- **Проверка:** Сервер стартует с dual-write repository
- **Время:** 5 минут

**Итого Фаза 4:** ~25 минут

---

### Фаза 5: Миграция данных (День 2)

**Цель:** Перенести все эмбеддинги из PostgreSQL в Qdrant

**Задачи:**

#### 5.1 Применить SQL миграцию 010
- **Исполнитель:** Нора
- **Действия:**
  1. SSH на сервер
  2. Применить миграцию:
  ```bash
  docker exec -i athena-pg psql -U athena athene_memory < /path/to/010_qdrant_vector_store.sql
  ```
  3. Проверить:
  ```sql
  SELECT count(*) FROM qdrant_migration_status;
  SELECT * FROM verify_qdrant_migration();
  ```
- **Проверка:** Таблица qdrant_migration_status создана, все записи в статусе 'pending'
- **Откат:** `DROP TABLE IF EXISTS qdrant_migration_status;`
- **Время:** 5 минут

#### 5.2 Запустить миграцию данных
- **Исполнитель:** Нора
- **Действия:**
  1. Скопировать migrate_vectors_to_qdrant.py на сервер
  2. Установить зависимости: `pip install qdrant-client asyncpg pgvector python-dotenv`
  3. Запустить миграцию:
  ```bash
  python migrate_vectors_to_qdrant.py migrate
  ```
  4. Мониторить прогресс (ожидание: ~184 записи, ~1-2 минуты)
  5. После завершения запустить верификацию:
  ```bash
  python migrate_vectors_to_qdrant.py verify
  ```
- **Проверка:** verify показывает 100% migrated, Qdrant points count == PG vectors count
- **Откат:** `python migrate_vectors_to_qdrant.py rollback`
- **Время:** 5-10 минут

#### 5.3 Верификация миграции
- **Исполнитель:** Катерина
- **Действия:**
  1. Проверить количество в Qdrant:
  ```bash
  curl http://127.0.0.1:6333/collections/memories | python -m json.tool
  ```
  2. Проверить что search работает через Qdrant:
  ```bash
  curl -X POST http://127.0.0.1:6333/collections/memories/points/search \
    -H "Content-Type: application/json" \
    -d '{"vector": [0.1, 0.2, ...], "limit": 5}'
  ```
  3. Сравнить результаты поиска через athena-memory API с ожидаемыми
- **Проверка:** Количество точек совпадает, поиск возвращает релевантные результаты
- **Время:** 10 минут

**Итого Фаза 5:** ~25 минут

---

### Фаза 6: Переключение поиска (День 2)

**Цель:** Убедиться что поиск работает через Qdrant

**Задачи:**

#### 6.1 Тестирование поиска через API
- **Исполнитель:** Катерина
- **Действия:**
  1. Перезапустить athena-memory с dual-write repository
  2. Выполнить поиск через MCP API:
  ```
  curl -X POST http://127.0.0.1:8000/mcp \
    -H "Content-Type: application/json" \
    -d '{"method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "test"}}}'
  ```
  3. Проверить что результаты возвращаются
  4. Проверить latency (должен быть <100ms для HNSW vs ~500ms для sequential scan)
- **Проверка:** Поиск работает, latency улучшился
- **Время:** 10 минут

#### 6.2 Тестирование dual-write
- **Исполнитель:** Катерина
- **Действия:**
  1. Записать новую запись через memory_store
  2. Проверить что вектор попал в Qdrant:
  ```bash
  curl http://127.0.0.1:6333/collections/memories/points/<id>
  ```
  3. Проверить что метаданные попали в PostgreSQL:
  ```sql
  SELECT id, content, metadata FROM memories WHERE id = '<id>';
  ```
- **Проверка:** Данные в обоих хранилищах консистентны
- **Время:** 10 минут

#### 6.3 Прогон полного набора тестов
- **Исполнитель:** Катерина
- **Действия:**
  1. `cd /home/opencode/projects/selti`
  2. `python -m pytest tests/ -v --tb=short`
  3. Проверить что все тесты проходят
- **Проверка:** Все тесты зелёные
- **Время:** 10 минут

**Итого Фаза 6:** ~30 минут

---

### Фаза 7: Удаление pgvector (День 3)

**Цель:** Убрать pgvector из кодовой базы

**Задачи:**

#### 7.1 Убрать pgvector из requirements.txt
- **Исполнитель:** Сона
- **Файл:** `requirements.txt`
- **Действия:**
  1. Убрать строку: `pgvector>=0.3.0`
  2. Убедиться что qdrant-client есть
- **Проверка:** `pip install -r requirements.txt` проходит
- **Время:** 5 минут

#### 7.2 Убрать pgvector из pool.py
- **Исполнитель:** Сона
- **Файл:** `memory_server/db/pool.py`
- **Действия:**
  1. Убрать все упоминания pgvector (уже сделано в 3.2)
  2. Проверить что файл чистый
- **Проверка:** `grep -r "pgvector\|register_vector" memory_server/` — пусто
- **Время:** 5 минут

#### 7.3 Убрать старый repository.py
- **Исполнитель:** Сона
- **Файл:** `memory_server/memory/repository_pgvector.py`
- **Действия:**
  1. Удалить: `rm repository_pgvector.py`
  2. Git rm: `git rm repository_pgvector.py`
- **Проверка:** В папке memory только repository.py (от Qdrant)
- **Время:** 2 минуты

#### 7.4 Убрать SEARCH_MEMORIES из queries.py
- **Исполнитель:** Сона
- **Файл:** `memory_server/db/queries.py`
- **Действия:**
  1. Убрать SEARCH_MEMORIES (sequential scan) — больше не нужен
  2. Оставить комментарий что поиск через Qdrant
- **Проверка:** Нет ссылок на SEARCH_MEMORIES в коде
- **Время:** 5 минут

#### 7.5 Применить SQL миграцию 011
- **Исполнитель:** Нора
- **Действия:**
  1. **ВНИМАНИЕ:** Применять ТОЛЬКО после 24 часов наблюдения!
  2. SSH на сервер
  3. Применить:
  ```bash
  docker exec -i athena-pg psql -U athena athene_memory < /path/to/011_drop_pgvector.sql
  ```
  4. Выполнить VACUUM:
  ```bash
  docker exec athena-pg psql -U athena athene_memory -c "VACUUM ANALYZE memories;"
  ```
  5. Проверить размер таблицы:
  ```sql
  SELECT pg_size_pretty(pg_total_relation_size('memories'));
  ```
- **Проверка:** Колонка embedding удалена, extension vector удалён, размер таблицы уменьшился
- **Откат:** См. комментарии в 011_drop_pgvector.sql
- **Время:** 5 минут

**Итого Фаза 7:** ~25 минут

---

### Фаза 8: Docker образ postgres:18 (День 3)

**Цель:** Заменить pgvector образ на обычный postgres

**Задачи:**

#### 8.1 Обновить docker-compose.yml
- **Исполнитель:** Рэй
- **Файл:** `docker-compose.yml`
- **Действия:**
  1. Заменить образ postgres:
  ```yaml
  postgres:
    image: postgres:18
  ```
  2. Убрать профиль local-db (postgres должен работать всегда)
  3. Проверить что volumes и healthcheck остались
- **Проверка:** `docker compose config` проходит
- **Время:** 5 минут

#### 8.2 Пересоздать postgres контейнер
- **Исполнитель:** Рэй
- **Действия:**
  1. **ВНИМАНИЕ:** Это требует даунтайм PostgreSQL!
  2. Остановить athena-memory: `docker stop athena-memory`
  3. Остановить postgres: `docker compose down postgres`
  4. Запустить postgres: `docker compose up -d postgres`
  5. Проверить здоровье: `docker compose ps postgres`
  6. Проверить что данные на месте:
  ```bash
  docker exec athena-pg psql -U athena athene_memory -c "SELECT count(*) FROM memories;"
  ```
  7. Запустить athena-memory: `docker start athena-memory`
- **Проверка:** PostgreSQL работает на postgres:18, данные на месте
- **Откат:** Вернуть образ pgvector/pgvector:pg18
- **Время:** 10 минут

**Итого Фаза 8:** ~15 минут

---

### Фаза 9: Тестирование (День 3)

**Цель:** Полная проверка работоспособности

**Задачи:**

#### 9.1 Интеграционные тесты
- **Исполнитель:** Катерина
- **Действия:**
  1. Запустить все тесты: `python -m pytest tests/ -v`
  2. Проверить каждый инструмент:
     - memory_store: запись в PG + Qdrant
     - memory_search: поиск через Qdrant HNSW
     - memory_get: чтение из PG
     - memory_update: обновление в PG + Qdrant
     - memory_delete: удаление из PG + Qdrant
     - memory_list: список из PG
     - memory_recent: последние из PG
     - memory_forget: удаление из PG + Qdrant
     - memory_stats: статистика из PG
     - memory_find_similar: поиск через Qdrant
     - memory_get_relations: связи из PG
     - memory_add_relation: добавление связи в PG
     - memory_traverse: обход графа в PG
     - memory_graph_stats: статистика графа в PG
- **Проверка:** Все инструменты работают корректно
- **Время:** 20 минут

#### 9.2 Нагрузочное тестирование
- **Исполнитель:** Катерина
- **Действия:**
  1. 100 последовательных поисков
  2. 50 параллельных поисков
  3. 100 записей подряд
  4. Проверить latency и error rate
- **Проверка:** P95 latency <100ms для поиска, 0 ошибок
- **Время:** 15 минут

#### 9.3 Проверка мониторинга
- **Исполнитель:** Мая
- **Действия:**
  1. Проверить /health endpoint
  2. Проверить /metrics endpoint
  3. Убедиться что Qdrant метрики доступны
- **Проверка:** Мониторинг работает
- **Время:** 10 минут

**Итого Фаза 9:** ~45 минут

---

### Фаза 10: Деплой на ai.atom.ui (День 3)

**Цель:** Финальный деплой в продакшен

**Задачи:**

#### 10.1 Финальный бэкап
- **Исполнитель:** Рэй
- **Действия:**
  1. Бэкап PostgreSQL: `docker exec athena-pg pg_dump -U athena athene_memory > /opt/data/backup_final_$(date +%Y%m%d).sql`
  2. Бэкап Qdrant:
  ```bash
  curl -X POST http://127.0.0.1:6333/collections/memories/snapshots -o /opt/data/qdrant_snapshot_$(date +%Y%m%d).tar
  ```
- **Проверка:** Бэкапы созданы
- **Время:** 5 минут

#### 10.2 Обновить код на сервере
- **Исполнитель:** Рэй
- **Действия:**
  1. SSH на сервер
  2. `cd ~/app/selti`
  3. `git pull origin main`
  4. Проверить что repository.py — Qdrant версия
  5. Проверить что requirements.txt не содержит pgvector
- **Проверка:** Код обновлён
- **Время:** 5 минут

#### 10.3 Пересобрать Docker образ
- **Исполнитель:** Рэй
- **Действия:**
  1. `docker compose build memory-server`
  2. `docker compose up -d memory-server`
  3. Проверить логи: `docker compose logs -f memory-server`
- **Проверка:** Сервер стартует без ошибок
- **Время:** 10 минут

#### 10.4 Финальная верификация
- **Исполнитель:** Катерина
- **Действия:**
  1. Проверить /health
  2. Выполнить 5 поисковых запросов
  3. Записать 3 новые записи
  4. Проверить что всё работает
- **Проверка:** Все операции успешны
- **Время:** 10 минут

#### 10.5 Обновить документацию
- **Исполнитель:** Тиамат
- **Действия:**
  1. Обновить README.md: убрать pgvector, добавить Qdrant
  2. Обновить ARCHITECTURE.md: новая схема
  3. Обновить .env.example: добавить QDRANT_* переменные
- **Проверка:** Документация актуальна
- **Время:** 15 минут

**Итого Фаза 10:** ~45 минут

---

## Риски и митигации

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Потеря данных при миграции | Низкая | Критическое | Dual-write, бэкап перед каждой фазой, верификация после каждого шага |
| Qdrant недоступен | Низкая | Высокое | Fallback на pgvector пока `qdrant_enabled=false` |
| Несовместимость данных | Средняя | Высокое | Верификация: compare counts, test search results |
| Долгая миграция | Низкая | Среднее | 184 записи мигрируются за ~1-2 минуты |
| Проблемы с Docker | Средняя | Среднее | Пошаговый деплой, откат через `docker compose down` |
| Потеря connectivity | Низкая | Высокое | Бэкапы на внешнем носителе (не только на сервере) |

---

## Rollback процедура

### Откат на любом этапе до Фазы 7:
1. Остановить athena-memory: `docker stop athena-memory`
2. Вернуть repository.py: `git checkout HEAD~N -- memory_server/memory/repository.py`
3. Вернуть queries.py: `git checkout HEAD~N -- memory_server/db/queries.py`
4. Вернуть pool.py: `git checkout HEAD~N -- memory_server/db/pool.py`
5. Вернуть requirements.txt: `git checkout HEAD~N -- requirements.txt`
6. Пересобрать: `docker compose build memory-server`
7. Запустить: `docker compose up -d memory-server`
8. Откатить Qdrant миграцию: `python migrate_vectors_to_qdrant.py rollback`
9. Откатить SQL: `DROP TABLE IF EXISTS qdrant_migration_status;`

### Откат после Фазы 7 (удалён embedding):
1. Остановить athena-memory
2. Восстановить extension: `CREATE EXTENSION IF NOT EXISTS vector;`
3. Восстановить колонку: `ALTER TABLE memories ADD COLUMN embedding vector(4096);`
4. Импортировать данные из Qdrant: `python migrate_vectors_to_qdrant.py rollback`
5. Вернуть код
6. Пересобрать Docker образ с pgvector
7. Запустить

---

## Чеклист перед продакшеном

- [ ] Бэкап PostgreSQL сделан
- [ ] Qdrant контейнер запущен и здоров
- [ ] Коллекция memories создана с HNSW индексом
- [ ] Все 184 записи мигрированы в Qdrant
- [ ] verify_qdrant_migration() показывает 100% migrated
- [ ] Поиск через Qdrant работает (latency <100ms)
- [ ] Dual-write работает (новые записи попадают в оба хранилища)
- [ ] Все тесты проходят
- [ ] Нагрузочное тестирование пройдено
- [ ] Мониторинг работает (/health, /metrics)
- [ ] pgvector удалён из requirements.txt
- [ ] embedding колонка удалена из PostgreSQL
- [ ] Docker образ: postgres:18 (без pgvector)
- [ ] Документация обновлена
- [ ] Rollback процедура протестирована (на dev)

---

## Оценка трудозатрат

| Фаза | Время | Исполнители |
|------|-------|-------------|
| Фаза 0: Подготовка | 30 мин | Рэй, Катерина, Момо |
| Фаза 1: Инфраструктура | 40 мин | Рэй, Нора |
| Фаза 2: Конфигурация | 20 мин | Рэй, Сона |
| Фаза 3: Qdrant Client | 40 мин | Сона |
| Фаза 4: Dual-Write | 25 мин | Сона |
| Фаза 5: Миграция данных | 25 мин | Нора, Катерина |
| Фаза 6: Переключение поиска | 30 мин | Катерина |
| Фаза 7: Удаление pgvector | 25 мин | Сона, Нора |
| Фаза 8: Docker postgres:18 | 15 мин | Рэй |
| Фаза 9: Тестирование | 45 мин | Катерина, Мая |
| Фаза 10: Деплой | 45 мин | Рэй, Катерина, Тиамат |
| **ИТОГО** | **~5.5 часов** | |

---

## Зависимости между фазами

```
Фаза 0 (Подготовка)
    ↓
Фаза 1 (Qdrant контейнер)
    ↓
Фаза 2 (Конфигурация)
    ↓
Фаза 3 (Qdrant Client)
    ↓
Фаза 4 (Dual-Write)
    ↓
Фаза 5 (Миграция данных)
    ↓
Фаза 6 (Переключение поиска)
    ↓
    ↓ [наблюдение 24 часа]
    ↓
Фаза 7 (Удаление pgvector)
    ↓
Фаза 8 (Docker postgres:18)
    ↓
Фаза 9 (Тестирование)
    ↓
Фаза 10 (Деплой)
```

---

## Исполнители

| Исполнитель | Роль | Задачи |
|-------------|------|--------|
| **Рэй** | DevOps | Docker, инфраструктура, деплой |
| **Нора** | DB-Architect | SQL миграции, миграция данных |
| **Сона** | Programmer | Python код, repository, queries |
| **Катерина** | Tester | Тестирование, верификация |
| **Мая** | Observability | Мониторинг, метрики |
| **Тиамат** | Tech-Writer | Документация |
| **Момо** | Planner | Оркестрация, контроль |

---

*План готов к утверждению. Милорд, скажи когда начинать.* 🎯
