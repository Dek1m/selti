# ============================================================
# Архитектура миграции: pgvector → Qdrant
# ============================================================

## Проблема

pgvector 0.8.5 имеет **жёсткое ограничение в 2000 измерений** для HNSW/IVFFlat индексов.
Эмбеддинги qwen3-embedding-8b — **4096-dim**. Итого:

- **Индекс невозможно построить** → sequential scan на каждый поиск
- При 1.2M записей: latency **1-5 секунд** на запрос
- Нет масштабирования: рост записей = рост latency

## Решение

**Qdrant** — выделенное векторное хранилище с HNSW-индексом без ограничения dim.

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  PostgreSQL │     │    Qdrant    │     │    Redis     │
│  (metadata) │     │  (vectors)   │     │   (cache)    │
│             │     │              │     │              │
│  id, user,  │     │  id, vector  │     │  embedding   │
│  content,   │     │  payload:    │     │  cache       │
│  namespace, │     │  namespace,  │     │              │
│  relations  │     │  user_id     │     │              │
└─────────────┘     └──────────────┘     └──────────────┘
```

## Write Flow (новый)

```
memory_store(content, user_id, namespace)
    │
    ├─1→ embedding = embed(content)          # Embedding Service
    ├─2→ INSERT INTO memories (PG)           # Метаданные (без vector)
    ├─3→ qdrant.upsert(vector=embedding)     # Вектор в Qdrant
    └─4→ return memory_id
```

## Search Flow (новый)

```
memory_search(query, user_id, namespace)
    │
    ├─1→ embedding = embed(query)            # Embedding Service
    ├─2→ qdrant.search(                      # HNSW search
    │       vector=embedding,
    │       filter={namespace, user_id},
    │       limit=10
    │    ) → [{id, score}]
    ├─3→ SELECT * FROM memories              # Fetch metadata
    │    WHERE id IN (qdrant_ids)
    └─4→ merge(results, scores) → []SearchResult
```

## Performance

| Метрика           | pgvector (до)       | Qdrant (после)    |
|-------------------|---------------------|-------------------|
| Поиск (1.2M)      | 1-5 сек (seq scan)  | 5-50ms (HNSW)     |
| Запись (batch)     | ~10ms               | ~5ms              |
| Память (векторов)  | 20GB (RAM)          | 20GB (on-disk)    |
| Масштабирование   | линейное            | логарифмическое   |

## Файлы миграции

```
migrations/
├── 010_qdrant_vector_store.sql              # SQL: sync-таблица, верификация
├── migrate_vectors_to_qdrant.py             # Python: экспорт PG → импорт Qdrant
├── setup_qdrant_collection.py               # Python: создание коллекции
├── queries_v2_qdrant.sql                    # SQL: обновлённые запросы
├── repository_qdrant.py                     # Python: новый repository
└── qdrant_collection.yaml                   # YAML: конфигурация коллекции
```

## Пошаговая миграция

### Phase 1: Подготовка (до даунтайма)

```bash
# 1. Создать коллекцию Qdrant
python migrations/setup_qdrant_collection.py

# 2. Создать sync-таблицу в PostgreSQL
psql -f migrations/010_qdrant_vector_store.sql

# 3. Запустить миграцию данных (фоновый процесс)
python migrations/migrate_vectors_to_qdrant.py migrate
```

### Phase 2: Переключение (кратковременный даунтайм)

```bash
# 1. Остановить selti сервер
docker stop selti

# 2. Верифицировать миграцию
python migrations/migrate_vectors_to_qdrant.py verify

# 3. Обновить код: repository.py → repository_qdrant.py
# 4. Обновить queries.py (убрать embedding из SQL)
# 5. Обновить pool.py (убрать register_vector)
# 6. Запустить selti с новым кодом
docker start selti
```

### Phase 3: Очистка (после стабилизации)

```bash
# 1. Удалить колонку embedding из PostgreSQL
python migrations/migrate_vectors_to_qdrant.py cleanup
```

## Rollback процедура

### Откат ДО удаления колонки embedding:

```bash
# 1. Остановить сервер
docker stop selti

# 2. Откатить миграцию данных
python migrations/migrate_vectors_to_qdrant.py rollback

# 3. Вернуть старый код (repository.py без Qdrant)
git checkout HEAD~1 -- memory_server/memory/repository.py

# 4. Запустить сервер
docker start selti
```

### Откат ПОСЛЕ удаления колонки embedding:

```bash
# 1. Остановить сервер
docker stop selti

# 2. Вернуть колонку embedding
psql -c "ALTER TABLE memories ADD COLUMN embedding vector(4096);"

# 3. Импортировать вектора обратно из Qdrant
python migrations/migrate_vectors_to_qdrant.py rollback

# 4. Вернуть старый код
git checkout HEAD~1 -- memory_server/memory/repository.py
git checkout HEAD~1 -- memory_server/db/pool.py

# 5. Запустить сервер
docker start selti
```

## ON CONFLICT / Upsert паттерн

### Текущий (pgvector):

```sql
INSERT INTO memories (user_id, content, embedding, metadata, ...)
VALUES ($1, $2, $3::vector, $4::jsonb, ...)
ON CONFLICT (namespace, content_hash)
DO UPDATE SET metadata = memories.metadata || EXCLUDED.metadata
RETURNING id
```

### Новый (Qdrant):

```python
# Шаг 1: PostgreSQL (metadata + dedup)
memory_id = await conn.fetchrow(
    """INSERT INTO memories (user_id, content, metadata, ...)
       VALUES ($1, $2, $3::jsonb, ...)
       ON CONFLICT (namespace, content_hash)
       DO UPDATE SET ...
       RETURNING id""",
    ...
)

# Шаг 2: Qdrant (vector upsert — идемпотентен по id)
qdrant.upsert(
    collection_name="memories",
    points=[PointStruct(id=memory_id, vector=embedding, payload={...})]
)
```

**Ключевое:** Qdrant upsert по UUID — **идемпотентен**. Повторный вызов с тем же ID перезаписывает данные. Это безопаснее ON CONFLICT в PostgreSQL.

## Индексы

### PostgreSQL (без изменений):

```sql
-- Фильтрация (B-tree)
CREATE INDEX idx_memories_user_id    ON memories (user_id);
CREATE INDEX idx_memories_namespace  ON memories (namespace);
CREATE INDEX idx_memories_created_at ON memories (created_at DESC);

-- Dedup
CREATE UNIQUE INDEX idx_memories_content_hash
    ON memories (namespace, content_hash)
    WHERE content_hash IS NOT NULL;
```

### Qdrant (payload indexes):

```python
# Ускоряют фильтрацию при vector search
client.create_payload_index("memories", "namespace",  KEYWORD)
client.create_payload_index("memories", "user_id",    KEYWORD)
client.create_payload_index("memories", "importance", INTEGER)
```

## Rationale

### Почему Qdrant, а не альтернативы:

| Решение         | Плюс                          | Минус                         |
|-----------------|-------------------------------|-------------------------------|
| pgvector halfvec | Один PG                       | Потеря точности fp16          |
| pgvectorscale   | DiskANN, без лимита dim       | Нужен отдельный extension     |
| Qdrant          | HNSW без лимита, on-disk     | Доп. сервис, сеть             |
| Weaviate        | GraphQL API                   | Сложнее интеграция            |
| Milvus          | GPU-accelerated              | Тяжёлый (etcd, MinIO)        |

**Qdrant chosen потому что:**
1. HNSW без ограничения dim (4096 OK)
2. On-disk вектора (20GB не влезут в RAM)
3. Payload фильтрация (namespace, user_id) прямо в поиске
4. Простой REST API (docker one-liner)
5. Python SDK (qdrant-client) — 5 строк для интеграции
