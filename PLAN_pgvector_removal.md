# План: Полный уход от pgvector → Qdrant-only

**Статус:** В работе  
**Дата:** 2026-07-30  
**Автор:** Нора (DB-Architect)  
**Предусловие:** Все эмбеддинги мигрированы в Qdrant, verify_qdrant_migration() = 100%

---

## 1. SQL миграция

**Файл:** `migrations/011_drop_pgvector.sql` — ✅ создан

### Что делает (в транзакции):
1. `DROP INDEX IF EXISTS idx_memories_embedding_ivfflat` — safety net
2. `DROP INDEX IF EXISTS idx_memories_embedding_hnsw` — safety net
3. `ALTER TABLE memories DROP COLUMN IF EXISTS embedding` — удаление 16KB/запись
4. `DROP EXTENSION IF EXISTS vector` — удаление pgvector
5. `DROP FUNCTION IF EXISTS search_memories_approx` —依赖ует на `<=>`
6. Очистка журнала миграции (view, function, table)
7. `COMMIT`
8. Вне транзакции: `VACUUM ANALYZE memories`

### Экономика:
- ~16KB × 1.2M записей = **~19 ГБ диска** возвращается
- PostgreSQL перестаёт грузить vector extension (~2MB RAM)

---

## 2. Python-файлы: конкретные правки

### 2.1. `requirements.txt`

**Убрать строку:**
```
pgvector>=0.3.0
```

---

### 2.2. `memory_server/db/pool.py`

**Текущий код (строки 4, 23):**
```python
from pgvector.asyncpg import register_vector  # ← УБРАТЬ
...
await register_vector(conn)  # ← УБРАТЬ
```

**Новый код:**
```python
import json

import asyncpg


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 20,
) -> asyncpg.Pool:
    """Создаёт пул соединений к PostgreSQL (без pgvector)."""
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    async def init_conn(conn: asyncpg.Connection) -> None:
        """Инициализация каждого нового соединения."""
        # Регистрируем JSONB codec
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        await conn.execute("SET statement_timeout = '30s'")

    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        init=init_conn,
        timeout=10.0,
    )
```

**Изменения:**
- Удалён импорт `from pgvector.asyncpg import register_vector`
- Удалён вызов `await register_vector(conn)`
- Обновлён docstring

---

### 2.3. `memory_server/db/queries.py`

**INSERT_MEMORY** (строки 1-5):
```python
INSERT_MEMORY = """
    INSERT INTO memories (user_id, content, metadata, namespace, namespace_id, content_hash, importance)
    VALUES ($1, $2, $3::jsonb, $4, $5::uuid, $6, $7)
    RETURNING id
"""
```
- Убрана колонка `embedding`
- Убран каст `$3::vector` → теперь `$3::jsonb` для metadata

**INSERT_MEMORY_BATCH** (строки 7-19):
```python
INSERT_MEMORY_BATCH = """
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
"""
```
- Убрана колонка `embedding`
- Убран `unnest($3::text[])::vector`
- Сдвиг параметров: metadata теперь $3, namespace $4, namespace_id $5, content_hash $6, importance $7

**SEARCH_MEMORIES** (строки 38-48) — **ПОЛНОСТЬЮ УДАЛИТЬ**:
```python
# УДАЛИТЬ ВЕСЬ БЛОК — поиск теперь через Qdrant API
```

**UPDATE_MEMORY** (строки 50-59):
```python
UPDATE_MEMORY = """
    UPDATE memories
    SET content = COALESCE($2, content),
        metadata = COALESCE($3::jsonb, metadata),
        importance = COALESCE($4, importance),
        updated_at = now()
    WHERE id = $1
    RETURNING id, user_id, content, metadata, namespace, importance, created_at, updated_at, content_hash
"""
```
- Убрана колонка `embedding`
- Убран каст `$3::vector`
- Сдвиг параметров: metadata теперь $3, importance $4

**Сдвиг параметров в UPDATE_MEMORY:**
| Было | Стало |
|------|-------|
| $1 = memory_id | $1 = memory_id |
| $2 = content | $2 = content |
| $3 = embedding | $3 = metadata |
| $4 = metadata | $4 = importance |
| $5 = importance | — |

---

### 2.4. `memory_server/memory/repository.py`

**`insert()`** (строки 26-49):
```python
async def insert(
    self,
    user_id: str,
    content: str,
    # embedding: list[float],  ← УБРАТЬ
    metadata: dict,
    namespace: str,
    namespace_id: str,
    content_hash: str | None = None,
    importance: int = 3,
) -> str:
    async with self.pool.acquire() as conn:
        row = await conn.fetchrow(
            q.INSERT_MEMORY,
            user_id,
            content,
            # embedding,  ← УБРАТЬ
            metadata,
            namespace,
            namespace_id,
            content_hash,
            importance,
        )
        return row["id"]
```

**`insert_batch()`** (строки 51-81):
```python
async def insert_batch(
    self,
    user_ids: list[str],
    contents: list[str],
    # embeddings: list[str],  ← УБРАТЬ
    metadatas: list[dict],
    namespaces: list[str],
    namespace_ids: list[str],
    content_hashes: list[str | None],
    importances: list[int] | None = None,
) -> list[str]:
    if importances is None:
        importances = [3] * len(user_ids)
    async with self.pool.acquire() as conn:
        rows = await conn.fetch(
            q.INSERT_MEMORY_BATCH,
            user_ids,
            contents,
            # embeddings,  ← УБРАТЬ
            metadatas,
            namespaces,
            namespace_ids,
            content_hashes,
            importances,
        )
        return [str(row["id"]) for row in rows]
```

**`search()`** (строки 125-151) — **ПОЛНОСТЬЮ УДАЛИТЬ МЕТОД**:
```python
# search() теперь через Qdrant API, не через SQL
# Удалить метод целиком
```

**`update()`** (строки 153-182):
```python
async def update(
    self,
    memory_id: str,
    content: str | None = None,
    # embedding: list[float] | None = None,  ← УБРАТЬ
    metadata: dict | None = None,
    importance: int | None = None,
) -> MemoryRecord | None:
    async with self.pool.acquire() as conn:
        row = await conn.fetchrow(
            q.UPDATE_MEMORY,
            memory_id,
            content,
            # embedding,  ← УБРАТЬ
            metadata,
            importance,
        )
        if row is None:
            return None
        return MemoryRecord(
            id=str(row["id"]),
            user_id=row["user_id"],
            content=row["content"],
            metadata=row["metadata"] or {},
            namespace=row["namespace"],
            importance=row["importance"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content_hash=row["content_hash"],
        )
```

---

### 2.5. `memory_server/memory/service.py`

**`store()`** (строки 41-100):
```python
async def store(
    self,
    content: str,
    user_id: str,
    metadata: dict | None = None,
    namespace: str | None = None,
    importance: int | None = None,
) -> tuple[MemoryRecord, DedupAction]:
    namespace = namespace or "default"
    ns_record = await self.ns_repo.get_or_create(namespace)
    content_hash: str | None = None
    # embedding: list[float] | None = None  ← УБРАТЬ

    if self.config.dedup_enabled:
        decision = await self.dedup.check(content, user_id, namespace, metadata=metadata)
        content_hash = decision.content_hash
        # embedding = decision.embedding  ← УБРАТЬ
        ...
        if decision.action == DedupAction.UPDATE:
            ...
            updated = await self.repository.update(
                memory_id=decision.existing_id,
                metadata={**record.metadata, **(metadata or {})},
                # embedding=decision.embedding,  ← УБРАТЬ (не передаём)
            )
            ...

    # Используем кэшированный эмбеддинг, или генерируем новый
    # if embedding is None:  ← УБРАТЬ
    #     embedding = await self.embedding.embed(content)  ← УБРАТЬ
    memory_id = await self.repository.insert(
        user_id=user_id,
        content=content,
        # embedding=embedding,  ← УБРАТЬ
        metadata=metadata or {},
        namespace=namespace,
        namespace_id=ns_record.id,
        content_hash=content_hash,
        importance=importance or 3,
    )
    ...
```

**`search()`** (строки 102-121):
```python
async def search(
    self,
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
    namespace: str | None = None,
) -> list[SearchResult]:
    # Теперь ищем через Qdrant, а не через pgvector
    query_embedding = await self.embedding.embed(query)
    # TODO: интеграция с Qdrant client (пока fallback на пустой список)
    # results = await qdrant_client.search(...)
    # records = await self.repository.get_by_ids([r.id for r in results])
    # return merge(records, results)
    logger.warning("search: Qdrant integration pending, returning empty")
    return []
```

**`update()`** (строки 132-155):
```python
async def update(
    self,
    memory_id: str,
    content: str | None = None,
    metadata: dict | None = None,
    importance: int | None = None,
) -> MemoryRecord:
    # embedding = None  ← УБРАТЬ
    # if content is not None:
    #     embedding = await self.embedding.embed(content)  ← УБРАТЬ
    record = await self.repository.update(
        memory_id=memory_id,
        content=content,
        # embedding=embedding,  ← УБРАТЬ
        metadata=metadata,
        importance=importance,
    )
    if record is None:
        raise NotFoundError(memory_id)
    return record
```

---

### 2.6. `memory_server/memory/dedup.py`

**`check()`** (строки 40-110):
- Метод `check()` вызывает `self.repository.search()` — **теперь это через Qdrant**
- Нужно заменить вызов `self.repository.search()` на Qdrant-поиск
- Или пока оставить как есть (будет пустой результат → semantic dedup не сработает → fallback на exact-only)

**Минимальное изменение:**
```python
# В check(), semantic dedup (строки 68-102):
# Заменить:
results = await self.repository.search(
    query_embedding=vector,
    user_id=user_id,
    namespace=namespace,
    threshold=threshold,
    limit=5,
)
# На Qdrant-поиск (когда будет интеграция)
# Пока: пропускаем semantic dedup
logger.info("Semantic dedup: Qdrant integration pending, skipping")
results = []
```

---

### 2.7. `memory_server/tools/memory_tools.py`

**`memory_ingest_batch()`** (строки 127-268):
- Строка 180: `"embedding": decision.embedding if service.config.dedup_enabled else None` — **УБРАТЬ**
- Строки 193-207: Логика кэширования embedding — **УБРАТЬ** (embeddings больше не хранятся в PG)
- Строка 227: `embeddings_list = [str(item["embedding"]) for item in to_insert]` — **УБРАТЬ**
- Строка 242-251: Вызов `insert_batch` без embeddings — **ОБНОВИТЬ**

**Конкретные правки:**
```python
# Строка 180: убрать embedding из to_insert
to_insert.append({
    "content": entry["content"],
    "metadata": entry_metadata or {},
    "namespace": ns,
    "importance": entry.get("importance", 3),
    "content_hash": decision.content_hash if service.config.dedup_enabled else None,
    # "embedding": ...  ← УБРАТЬ
})

# Строки 186-210: УБРАТЬ ВЕСЬ БЛОК (Phase 2: batch embed)
# embeddings больше не хранятся в PG, их не нужно передавать в insert_batch

# Строки 224-251: Обновить вызов insert_batch
ids = await service.repository.insert_batch(
    user_ids=user_ids,
    contents=contents,
    # embeddings=embeddings_list,  ← УБРАТЬ
    metadatas=metadatas_list,
    namespaces=namespaces_list,
    namespace_ids=namespace_ids,
    content_hashes=content_hashes_list,
    importances=importances_list,
)
```

---

### 2.8. `docker-compose.yml`

**Строка 62:**
```yaml
# Было:
image: pgvector/pgvector:pg17
# Стало:
image: postgres:17
```

**Комментарий на строке 57 обновить:**
```yaml
# Было:
# PostgreSQL 17 + pgvector
# Стало:
# PostgreSQL 17
```

---

### 2.9. `memory_server/config.py`

**Строка 29:**
```python
# Было:
qdrant_enabled: bool = True  # False = fallback на pgvector
# Стало:
qdrant_enabled: bool = True  # Единственный векторный бэкенд
```

---

## 3. Порядок действий

### Перед миграцией (проверки):
1. ✅ `verify_qdrant_migration()` = 100% migrated
2. ✅ Все тесты с Qdrant-поиском проходят
3. ✅ Минимум 24 часа наблюдения
4. ✅ Бэкап БД: `pg_dump -Fc athena_memory > backup_before_pgvector_drop.dump`

### Применение:
1. **Остановить selti сервер** (docker compose stop memory-server)
2. **Выполнить SQL миграцию:**
   ```bash
   psql -U athena -d athena_memory -f migrations/011_drop_pgvector.sql
   psql -U athena -d athena_memory -c "VACUUM ANALYZE memories;"
   ```
3. **Применить Python-правки** (см. секцию 2)
4. **Обновить Docker:**
   ```bash
   # В docker-compose.yml: postgres:17 вместо pgvector/pgvector:pg17
   docker compose build memory-server  # пересобрать без pgvector
   ```
5. **Убрать pgvector из requirements.txt**
6. **Запустить selti:**
   ```bash
   docker compose up -d
   ```
7. **Верификация:**
   ```bash
   curl http://localhost:8000/health
   # Проверить что расширение vector удалено:
   psql -U athena -d athena_memory -c "SELECT * FROM pg_extension WHERE extname = 'vector';"
   # Должен вернуть 0 строк
   ```

---

## 4. Rollback процедура

### Если миграция НЕ применена (SQL не выполнен):
1. `git revert` всех Python-изменений
2. Пересобрать Docker образ
3. Запустить selti

### Если миграция ПРИМЕНЕНА (SQL выполнен):
1. **Остановить selti сервер**
2. **Восстановить extension:**
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
3. **Восстановить колонку** (данные потеряны — нужен импорт из Qdrant):
   ```sql
   ALTER TABLE memories ADD COLUMN embedding vector(4096);
   ```
4. **Восстановить функцию:**
   ```sql
   CREATE OR REPLACE FUNCTION search_memories_approx(
       p_user_id TEXT,
       p_embedding vector(4096),
       p_threshold FLOAT DEFAULT 0.7,
       p_limit INT DEFAULT 20
   )
   RETURNS TABLE(
       id UUID, user_id TEXT, content TEXT, metadata JSONB, score FLOAT
   )
   LANGUAGE SQL STABLE
   AS $$
       SELECT m.id, m.user_id, m.content, m.metadata,
              1 - (m.embedding <=> p_embedding) AS score
       FROM memories m
       WHERE m.user_id = p_user_id
         AND 1 - (m.embedding <=> p_embedding) >= p_threshold
       ORDER BY m.embedding <=> p_embedding
       LIMIT p_limit;
   $$;
   ```
5. **Импортировать эмбеддинги из Qdrant** (Python скрипт `migrate_vectors_to_qdrant.py --rollback`)
6. **Откатить Python-код:** `git revert`
7. **Восстановить docker-compose.yml:** `pgvector/pgvector:pg17`
8. **Пересобрать и запустить:**
   ```bash
   docker compose build && docker compose up -d
   ```

### Из БД-бэкапа (самый надёжный):
```bash
pg_restore -U athena -d athena_memory backup_before_pgvector_drop.dump
```

---

## 5. Risk Matrix

| Риск | Вероятность | Влияние | Mitigation |
|------|-------------|---------|------------|
| Потеря данных при DROP COLUMN | Низкая | Критическое | Бэкап перед миграцией |
| Semantic dedup временно не работает | Высокая | Низкое | Exact dedup продолжает работать |
| Qdrant search не интегрирован | Средняя | Среднее | search() возвращает [] (graceful degradation) |
| Тесты падают | Средняя | Среднее | Обновить тесты параллельно |
| Docker build падает без pgvector | Низкая | Низкое | requirements.txt обновляется первым |

---

## 6. Тесты: необходимые правки

### 5.1. `tests/test_repository.py`

**TestInsert.test_insert_returns_id** (строки 27-47):
- Убрать `embedding=[0.1, 0.2, 0.3]` из вызова `repo.insert()`
- Убрать `[0.1, 0.2, 0.3]` из `assert_awaited_once_with()`

**TestSearch** (строки 107-169) — **УДАЛИТЬ ВЕСЬ КЛАСС**:
- `test_search_returns_results` — удалить
- `test_search_without_namespace` — удалить
- (search теперь через Qdrant, unit-тесты не нужны)

**TestUpdate.test_update_full** (строки 174-206):
- Убрать `embedding=[0.5, 0.6, 0.7]` из вызова `repo.update()`
- Обновить `assert_awaited_once_with()` — убрать embedding, сдвинуть параметры:
  ```python
  conn.fetchrow.assert_awaited_once_with(
      q.UPDATE_MEMORY,
      "mem-1",
      "new content",
      {"k": "v"},    # metadata теперь $3
      None,           # importance теперь $4
  )
  ```

**TestUpdate.test_update_partial** (строки 208-213):
- Убрать `embedding=None` из вызова `repo.update()`

### 5.2. `tests/test_service.py`

**TestStore.test_store_generates_embedding_and_returns_record** (строки 26-62):
- Строка 38: `service.embedding.embed = AsyncMock(...)` — **ОСТАВИТЬ** (embedding всё ещё генерируется для Qdrant)
- Строка 50: `service.embedding.embed.assert_awaited_once_with(...)` — **ОСТАВИТЬ**
- Строка 51-60: Обновить `assert_awaited_once_with()` — убрать `embedding=[0.1, 0.2, 0.3]`:
  ```python
  service.repository.insert.assert_awaited_once_with(
      user_id="u1",
      content="Hello world",
      # embedding убран
      metadata={"source": "test"},
      namespace="ns1",
      namespace_id="00000000-0000-0000-0000-bfed25f845e5",
      content_hash=None,
      importance=3,
  )
  ```

**TestStore.test_store_uses_default_metadata_and_namespace** (строки 64-91):
- Строка 67: `service.embedding.embed = AsyncMock(...)` — **ОСТАВИТЬ**
- Строка 82-91: Обновить `assert_awaited_once_with()` — убрать `embedding=[0.0, 0.0, 0.0]`

**TestStore.test_store_raises_if_get_returns_none** (строки 93-100):
- Строка 95: `service.embedding.embed = AsyncMock(...)` — **ОСТАВИТЬ** (но в new service.store() embedding не передаётся в insert)

**TestSearch.test_search_generates_query_embedding** (строки 104-128):
- Переписать тест: search теперь через Qdrant, не через repository.search()
- Либо удалить (пока Qdrant не интегрирован)

**TestUpdate.test_update_with_content_regenerates_embedding** (строки 158-181):
- Строка 168: `service.embedding.embed = AsyncMock(...)` — **ОСТАВИТЬ** (embedding генерируется для Qdrant)
- Строка 173: `service.embedding.embed.assert_awaited_once_with(...)` — **ОСТАВИТЬ**
- Строка 174-180: Обновить `assert_awaited_once_with()` — убрать `embedding=[0.9, 0.8, 0.7]`:
  ```python
  service.repository.update.assert_awaited_once_with(
      memory_id="mem-1",
      content="updated",
      # embedding убран
      metadata={"k": "v"},
      importance=None,
  )
  ```

**TestUpdate.test_update_without_content_skips_embedding** (строки 183-205):
- Строка 197: `service.embedding.embed.assert_not_awaited()` — **ОСТАВИТЬ**
- Строка 198-204: Обновить `assert_awaited_once_with()` — убрать `embedding=None`:
  ```python
  service.repository.update.assert_awaited_once_with(
      memory_id="mem-1",
      content=None,
      # embedding убран
      metadata={"k": "v"},
      importance=None,
  )
  ```

### 5.3. `tests/test_tools.py`

**memory_ingest_batch** (строки 49-50, 86, 108):
- `service.embedding.embed_many = AsyncMock(...)` — **ОСТАВИТЬ** (embedding нужен для Qdrant)
- Убрать `embedding` из проверяемых аргументов insert_batch

### 5.4. `tests/conftest.py`

- `mock_embedding_provider` (строки 43-44) — **ОСТАВИТЬ** (протокол не меняется)
- `mock_service` (строки 88-92) — **ОСТАВИТЬ** (сервис всё ещё принимает embedding_provider)

### 5.5. `tests/test_dedup.py`

- Все тесты с `dedup_engine.embedding.embed = AsyncMock(...)` — **ОСТАВИТЬ**
- Тесты semantic dedup: пока Qdrant не интегрирован, semantic dedup не работает → тесты могут упасть
- **Решение:** временно пропустить semantic dedup тесты или обновить mock

---

## 7. Итого: порядок коммитов

| # | Коммит | Файлы |
|---|--------|-------|
| 1 | SQL миграция | `migrations/011_drop_pgvector.sql` |
| 2 | Убрать pgvector из Python | `pool.py`, `queries.py`, `repository.py`, `service.py`, `dedup.py`, `memory_tools.py` |
| 3 | Обновить тесты | `test_repository.py`, `test_service.py`, `test_tools.py`, `test_dedup.py` |
| 4 | Docker + requirements | `docker-compose.yml`, `requirements.txt`, `config.py` |
| 5 | Документация | `PLAN_pgvector_removal.md` |
