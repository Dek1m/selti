# План: Новый функционал selti

> Декомпозиция по данным от Эны (архитектура), Рэй (инфраструктура), Сона (ТЗ)
> Текущая версия: v0.11.2, Python 3.12, PostgreSQL 16 + pgvector 0.7, Qdrant, Redis 7, Celery, FastMCP
> Цель: 7 новых MCP tools, 4 новые таблицы, 8 новых модулей

---

## Фаза 0: Инфраструктура и подготовка (P0) — 2-3 дня

Расширение сервера, настройка CI/CD, миграции — всё что блокирует остальные фазы.

### Шаг 0.1: Расширение сервера ai.atom.ui

- **Файлы:** инфраструктура (вне кода)
- **Что делаем:** Текущий сервер: 8 cores, 7.5G RAM — НЕ достаточен. Заказываем: 16G RAM (минимум), NVMe SSD. BGE-M3 (embeddings) и BGE-Reranker-v2-m3 (reranking) требуют ~8GB RAM только для моделей. PG + Redis + Qdrant + Celery — ещё ~4GB
- **Зависимости:** —
- **Время:** 1-2 дня (ожидание провайдера)
- **Риск:** Высокий. Задержка с сервером блокирует ВСЕ фазы. **Митигация:** Начать с локальной разработки (Docker), деплоить после расширения
- **Стоимость:** $75-220/мес (вертикальное расширение)

### Шаг 0.2: CI/CD pipeline — GitHub Actions

- **Файлы:** `.github/workflows/ci.yml` (новый), `.github/workflows/deploy.yml` (новый)
- **Что делаем:**
  - `ci.yml`: lint (ruff), type-check (mypy), тесты (pytest), security scan (bandit)
  - `deploy.yml`: build Docker → push → deploy на ai.atom.ui (SSH)
  - Secrets: `DATABASE_URL`, `REDIS_URL`, `QDRANT_URL`, `SSH_KEY`
  - Branch protection: main → PR required, 1 approval
- **Зависимости:** —
- **Время:** 2-3 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** Автоматическая проверка каждого PR, деплой одной кнопкой

### Шаг 0.3: Миграции — 4 новые таблицы

- **Файлы:** `migrations/016_access_policies.sql` (новый), `migrations/017_audit_log.sql` (новый), `migrations/018_entity_registry.sql` (новый), `migrations/019_bitemporal.sql` (новый)
- **Что делаем:**
  ```sql
  -- 016: ACL
  CREATE TABLE access_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace TEXT NOT NULL,
    role TEXT NOT NULL,           -- 'admin', 'reader', 'writer', 'guest'
    permission TEXT NOT NULL,     -- 'read', 'write', 'delete', 'admin'
    user_id TEXT,                 -- NULL = для всех пользователей роли
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(namespace, role, permission, user_id)
  );

  CREATE TABLE namespace_acl_defaults (
    namespace TEXT PRIMARY KEY,
    default_role TEXT NOT NULL DEFAULT 'reader',
    created_at TIMESTAMPTZ DEFAULT now()
  );

  -- 017: Audit log
  CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT now(),
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,         -- 'store', 'update', 'delete', 'search', etc.
    resource_id TEXT,
    namespace TEXT,
    details JSONB,
    ip_address INET
  );
  CREATE INDEX idx_audit_timestamp ON audit_log(timestamp);
  CREATE INDEX idx_audit_user ON audit_log(user_id);

  -- 018: Entity registry
  CREATE TABLE entity_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL,    -- 'person', 'project', 'module', 'class', etc.
    name TEXT NOT NULL,
    aliases TEXT[],               -- ['Серёжа', 'Sergey', 'Милорд']
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(entity_type, name)
  );
  CREATE INDEX idx_entity_aliases ON entity_registry USING GIN(aliases);

  -- 019: Bitemporal
  ALTER TABLE memories ADD COLUMN valid_from TIMESTAMPTZ DEFAULT now();
  ALTER TABLE memories ADD COLUMN valid_to TIMESTAMPTZ;  -- NULL = текущая версия
  ALTER TABLE memories ADD COLUMN transaction_time TIMESTAMPTZ DEFAULT now();
  CREATE INDEX idx_memories_valid ON memories(valid_from, valid_to);
  ```
- **Зависимости:** —
- **Время:** 2-3 часа
- **Риск:** Средний. Миграция на проде с данными. **Митигация:** Тестировать на копии БД, backup перед миграцией
- **Важно:** Миграции должны быть идемпотентны (IF NOT EXISTS)

### Шаг 0.4: Обновить requirements.txt

- **Файлы:** `requirements.txt`
- **Что делаем:** Добавить зависимости:
  ```
  psycopg2-binary>=2.9.9    # BM25 через pg_trgm
  pgvector>=0.3.0            # HNSW индексы (если ещё нет)
  cryptography>=43.0.0       # ACL шифрование
  ```
- **Зависимости:** —
- **Время:** 15 мин
- **Риск:** Низкий

---

## Фаза 1: Безопасность и аудит (P1) — 4-5 дней

ACL, audit log, entity registry — основа для всего остального функционала.

### Шаг 1.1: ACL модуль — `security/acl.py`

- **Файлы:** `memory_server/security/__init__.py` (новый), `memory_server/security/acl.py` (новый)
- **Что делаем:**
  - Класс `AccessController`:
    - `check_permission(user_id, namespace, action)` → bool
    - `get_user_role(user_id, namespace)` → str
    - `set_policy(namespace, role, permission, user_id=None)`
    - `get_policies(namespace)` → list[Policy]
  - Кэширование в Redis (TTL 5 минут)
  - Fallback: если policy нет — разрешить (backwards compatible)
  - Дефолтные роли из `namespace_acl_defaults`
- **Зависимости:** шаг 0.3 (таблица access_policies)
- **Время:** 3-4 часа
- **Риск:** Средний. Неправильный fallback может заблокировать всех. **Митигация:** Дефолт = разрешить, тестировать с разными ролями
- **Ожидаемый эффект:** Role-based access control для всех операций

### Шаг 1.2: Audit log модуль — `security/audit.py`

- **Файлы:** `memory_server/security/audit.py` (новый)
- **Что делаем:**
  - Класс `AuditWriter`:
    - `log(user_id, action, resource_id, namespace, details, ip_address=None)`
    - Асинхронная запись через Celery task (не блокирует основной поток)
    - Батчинг: буфер из 100 записей или 1 секунда — flush в PG
  - Метрики:
    - `selti_audit_events_total{action, namespace}` (counter)
    - `selti_audit_buffer_size` (gauge)
  - Миграция: `audit_log` таблица из шага 0.3
- **Зависимости:** шаг 0.3 (таблица audit_log)
- **Время:** 2-3 часа
- **Риск:** Низкий. Асинхронная запись не влияет на latency
- **Ожидаемый эффект:** Полный аудит всех операций с памятью

### Шаг 1.3: Интеграция ACL в MemoryService

- **Файлы:** `memory_server/memory/service.py`, `memory_server/tools/memory_tools.py`
- **Что делаем:**
  - В `service.py`: проверка ACL перед каждой операцией:
    - `store()` → permission='write'
    - `search()` → permission='read'
    - `update()` → permission='write'
    - `delete()` → permission='delete'
    - `forget()` → permission='admin'
  - В `memory_tools.py`: перед вызовом celery_call — проверка ACL
  - Если нет прав → `PermissionDeniedError` с понятным сообщением
  - Audit log для каждой операции (write, delete, admin)
- **Зависимости:** шаг 1.1 (ACL модуль), шаг 1.2 (audit)
- **Время:** 3-4 часа
- **Риск:** Средний. Ломает текущий flow без ACL. **Митигация:** Дефолтная policy = разрешить всё, включать ACL постепенно
- **Ожидаемый эффект:** Контроль доступа на уровне приложения

### Шаг 1.4: Entity registry модуль — `entity_linking/resolver.py` (базовый)

- **Файлы:** `memory_server/entity_linking/__init__.py` (новый), `memory_server/entity_linking/resolver.py` (новый)
- **Что делаем:**
  - Класс `EntityResolver`:
    - `register(entity_type, name, aliases=None, metadata=None)` → entity_id
    - `resolve(name)` → EntityRecord | None (поиск по name + aliases)
    - `find_similar(name, entity_type=None)` → list[EntityRecord]
    - `link(entity_id, memory_id)` → bool (связь entity → granule)
    - `get_linked(entity_id)` → list[MemoryRecord]
  - FTS через `pg_trgm` для нечёткого поиска по aliases
  - Кэширование в Redis (TTL 10 минут)
- **Зависимости:** шаг 0.3 (таблица entity_registry)
- **Время:** 3-4 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** Единый реестр сущностей (people, projects, modules) с автоматическим linking

### Шаг 1.5: MCP tool — `memory_set_access_policy`

- **Файлы:** `memory_server/tools/memory_tools.py`, `memory_server/tasks/memory_tasks.py`
- **Что делаем:**
  ```python
  @mcp.tool()
  async def memory_set_access_policy(
      namespace: str,
      role: str,
      permission: str,
      user_id: str | None = None,
  ) -> dict:
      """Set access policy for a namespace + role + permission."""
  ```
  - Делегирует в Celery task
  - Только admin может менять policies
  - Audit log: `policy_set`
- **Зависимости:** шаг 1.1 (ACL), шаг 1.2 (audit)
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Управление ACL через MCP

### Шаг 1.6: MCP tool — `memory_entity_link`

- **Файлы:** `memory_server/tools/memory_tools.py`, `memory_server/tasks/memory_tasks.py`
- **Что делаем:**
  ```python
  @mcp.tool()
  async def memory_entity_link(
      entity_name: str,
      entity_type: str,
      memory_id: str,
      aliases: list[str] | None = None,
  ) -> dict:
      """Link an entity to a memory granule. Auto-resolves existing entities."""
  ```
  - Автоматический register если entity не найдена
  - Audit log: `entity_linked`
- **Зависимости:** шаг 1.4 (EntityResolver)
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Автоматический linking сущностей с гранулами

### Шаг 1.7: Обновить промпт Тиши (akame)

- **Файлы:** `agents/memory-granulator.md` (в проекте akame)
- **Что делаем:**
  - Добавить описание новых tools: `memory_set_access_policy`, `memory_entity_link`
  - Добавить правила использования ACL
  - Обновить таблицу tools
  - Добавить entity linking в workflow грануляции
- **Зависимости:** шаги 1.5, 1.6 (tools должны быть готовы)
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Тишь знает о новых возможностях

---

## Фаза 2: Временны́е операции (P2) — 3-4 дня

Bitemporal versioning и запросы по временному срезу.

### Шаг 2.1: Bitemporal модуль — `temporal/bitemporal.py`

- **Файлы:** `memory_server/temporal/__init__.py` (новый), `memory_server/temporal/bitemporal.py` (новый)
- **Что делаем:**
  - Класс `BitemporalStore`:
    - `create_version(memory_id, content, metadata)` → новая версия с `valid_from=now()`, предыдущая → `valid_to=now()`
    - `get_current(memory_id)` → текущая версия (valid_to IS NULL)
    - `get_version(memory_id, timestamp)` → версия, действовавшая в указанный момент
    - `get_history(memory_id)` → все версии, отсортированные по valid_from
    - `expire(memory_id)` → установить valid_to для текущей версии
  - Триггер: при `update()` в service — автоматически создавать новую версию
  - Индекс: `CREATE INDEX idx_memories_temporal ON memories(id, valid_from, valid_to)`
- **Зависимости:** шаг 0.3 (миграция 019)
- **Время:** 3-4 часа
- **Риск:** Средний. Неправильная логика версионирования может потерять данные. **Митигация:** Unit-тесты на каждую операцию
- **Ожидаемый эффект:** Каждое изменение — новая версия, полная история

### Шаг 2.2: Интеграция bitemporal в MemoryService

- **Файлы:** `memory_server/memory/service.py`
- **Что делаем:**
  - При `update()`: автоматически вызывать `bitemporal.create_version()`
  - При `get_by_id()`: возвращать текущую версию (как сейчас)
  - Новый метод: `get_history(memory_id)` → list[MemoryRecord]
  - Новый метод: `get_version_at(memory_id, timestamp)` → MemoryRecord
- **Зависимости:** шаг 2.1 (BitemporalStore)
- **Время:** 2-3 часа
- **Риск:** Средний. Меняет поведение update(). **Митигация:** Фиче-флаг `TEMPORAL_ENABLED`, выключать при проблемах
- **Ожидаемый эффект:** История изменений для каждой гранулы

### Шаг 2.3: MCP tool — `memory_set_ttl`

- **Файлы:** `memory_server/tools/memory_tools.py`, `memory_server/tasks/memory_tasks.py`
- **Что делаем:**
  ```python
  @mcp.tool()
  async def memory_set_ttl(
      memory_id: str,
      valid_from: str | None = None,
      valid_to: str | None = None,
  ) -> dict:
      """Set bitemporal bounds for a memory record.
      
      valid_from: when this record becomes valid (ISO datetime)
      valid_to: when this record expires (ISO datetime, null = never)
      """
  ```
  - Audit log: `ttl_set`
- **Зависимости:** шаг 2.1 (BitemporalStore)
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Ручная настройка временны́х границ

### Шаг 2.4: MCP tool — `memory_query_temporal`

- **Файлы:** `memory_server/tools/memory_tools.py`, `memory_server/tasks/memory_tasks.py`
- **Что делаем:**
  ```python
  @mcp.tool()
  async def memory_query_temporal(
      timestamp: str,
      namespace: str | None = None,
      user_id: str | None = None,
      limit: int = 50,
  ) -> list[dict]:
      """Query memories as they were at a specific point in time.
      
      Returns records that were valid at the given timestamp.
      """
  ```
  - SQL: `WHERE valid_from <= $1 AND (valid_to IS NULL OR valid_to > $1)`
  - Audit log: `temporal_query`
- **Зависимости:** шаг 2.1 (BitemporalStore)
- **Время:** 1-2 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** «Что было в памяти 15 июля?» — ответ за миллисекунды

---

## Фаза 3: Продвинутый поиск (P3) — 5-6 дней

Hybrid search, BM25, graph rank fusion — самая сложная фаза.

### Шаг 3.1: BM25 модуль — `search/bm25.py`

- **Файлы:** `memory_server/search/__init__.py` (новый), `memory_server/search/bm25.py` (новый)
- **Что делаем:**
  - Класс `BM25Search`:
    - `search(query_text, user_id, namespace, limit)` → list[SearchResult]
    - Использует PostgreSQL `tsvector` + `ts_rank_cd`
    - Предобработка: LOWER,.stemming (pg_trgm)
    - Fallback: если нет tsvector — plain ILIKE
  - SQL:
    ```sql
    SELECT id, content, ts_rank_cd(to_tsvector('russian', content), plainto_tsquery('russian', $1)) AS rank
    FROM memories
    WHERE to_tsvector('russian', content) @@ plainto_tsquery('russian', $1)
    ORDER BY rank DESC LIMIT $2
    ```
  - Миграция: добавить tsvector колонку + индекс:
    ```sql
    ALTER TABLE memories ADD COLUMN content_tsv tsvector;
    CREATE INDEX idx_memories_tsv ON memories USING GIN(content_tsv);
    UPDATE memories SET content_tsv = to_tsvector('russian', content);
    ```
- **Зависимости:** —
- **Время:** 3-4 часа
- **Риск:** Средний. PostgreSQL full-text search может быть медленным на больших объёмах. **Митигация:** GIN индекс, тестирование с 10k+ записей
- **Ожидаемый эффект:** Качественный текстовый поиск по содержимому

### Шаг 3.2: Graph rank модуль — `search/graph_rank.py`

- **Файлы:** `memory_server/search/graph_rank.py` (новый)
- **Что делаем:**
  - Класс `GraphRanker`:
    - `rank(candidates, query_embedding)` → list[SearchResult] (переранжированные)
    - Алгоритм: PageRank-like centrality на графе связей
    - Учитывает: количество связей, вес рёбер, глубину от кандидата
    - Вес: `score_final = 0.7 * vector_score + 0.3 * centrality_score`
  - Вход: кандидаты из vector search (top 50)
  - Выход: переранжированные кандидаты (top 10)
- **Зависимости:** —
- **Время:** 3-4 часа
- **Риск:** Средний. PageRank может быть медленным на больших графах. **Митигация:** Ограничивать граф 50 кандидатами, кэшировать centrality
- **Ожидаемый эффект:** Связанные гранулы получают буст в результатах

### Шаг 3.3: Hybrid search модуль — `search/hybrid.py`

- **Файлы:** `memory_server/search/hybrid.py` (новый)
- **Что делаем:**
  - Класс `HybridSearch`:
    - `search(query_text, query_embedding, user_id, namespace, limit)` → list[SearchResult]
    - 3 этапа:
      1. Vector search (Qdrant) → top 50
      2. BM25 search (PG) → top 50
      3. Rank fusion (RRF — Reciprocal Rank Fusion):
         ```
         score = sum(1 / (k + rank_i)) for each result set
         k = 60 (константа RRF)
         ```
    - Финальный top 10 из объединённого списка
  - Опционально: graph rank поверх fusion (см. шаг 3.2)
- **Зависимости:** шаг 3.1 (BM25), шаг 3.2 (graph rank)
- **Время:** 3-4 часа
- **Риск:** Средний. RRF параметры требуют тюнинга. **Митигация:** Дефолтные параметры из статьи, A/B тестирование
- **Ожидаемый effet:** Лучший поиск: semantic + lexical + graph

### Шаг 3.4: Интеграция hybrid search в MemoryService

- **Файлы:** `memory_server/memory/service.py`, `memory_server/memory/repository_qdrant.py`
- **Что делаем:**
  - В `service.py`: новый метод `hybrid_search(query_text, query_embedding, ...)`
  - В `repository_qdrant.py`: метод `vector_search()` возвращает raw кандидатов
  - В `service.py`: координация vector + BM25 + fusion
  - Фиче-флаг: `HYBRID_SEARCH_ENABLED` (по умолчанию false)
- **Зависимости:** шаг 3.3 (HybridSearch)
- **Время:** 2-3 часа
- **Риск:** Средний. Меняет поведение поиска. **Митигация:** Фиче-флаг, fallback на vector search
- **Ожидаемый эффект:** Гибридный поиск доступен через API

### Шаг 3.5: MCP tool — `memory_hybrid_search`

- **Файлы:** `memory_server/tools/memory_tools.py`, `memory_server/tasks/memory_tasks.py`
- **Что делаем:**
  ```python
  @mcp.tool()
  async def memory_hybrid_search(
      query: str,
      user_id: str | None = None,
      limit: int = 10,
      threshold: float = 0.7,
      namespace: str | None = None,
      enable_graph_rank: bool = True,
  ) -> list[dict]:
      """Hybrid search: Vector + BM25 + Graph rank fusion.
      
      Combines semantic similarity, full-text search, and graph centrality
      for the best possible search results.
      """
  ```
  - Audit log: `hybrid_search`
  - Метрики: `selti_hybrid_search_duration_seconds`, `selti_hybrid_search_results_count`
- **Зависимости:** шаг 3.4 (интеграция)
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** Лучший поиск доступен через MCP

### Шаг 3.6: MCP tool — `memory_consolidate`

- **Файлы:** `memory_server/tools/memory_tools.py`, `memory_server/consolidation/__init__.py` (новый), `memory_server/consolidation/consolidator.py` (новый)
- **Что делаем:**
  ```python
  @mcp.tool()
  async def memory_consolidate(
      namespace: str | None = None,
      user_id: str | None = None,
      dry_run: bool = True,
  ) -> dict:
      """Consolidate memories: merge duplicates, archive outdated.
      
      dry_run: if true, only show what would be changed.
      Returns: {merged: N, archived: N, kept: N}
      """
  ```
  - Класс `MemoryConsolidator`:
    - `find_duplicates(namespace, threshold=0.95)` → list[tuple[MemoryRecord, MemoryRecord]]
    - `merge(primary, secondary)` → MemoryRecord (объединённый)
    - `archive_outdated(namespace, older_than_days=90)` → list[MemoryRecord]
    - `find_related(namespace, limit=50)` → list[tuple[MemoryRecord, MemoryRecord]] (для potential merge)
  - Алгоритм:
    1. Vector search с низким threshold (0.85) для поиска дубликатов
    2. Сравнение content_hash для точных дублей
    3. Объединение metadata при merge
    4. Архивация записей старше 90 дней с низким importance
- **Зависимости:** шаг 3.3 (hybrid search для поиска дублей)
- **Время:** 4-5 часов
- **Риск:** Высокий. Неправильный merge может потерять данные. **Митигация:** dry_run по умолчанию, backup перед merge, ручное подтверждение
- **Ожидаемый эффект:** Автоматическая чистка дублей и архивация старого

---

## Фаза 4: Тестирование и документация (P4) — 3-4 дня

Полное покрытие тестами, документация, финальная настройка.

### Шаг 4.1: Unit-тесты для всех новых модулей

- **Файлы:** `tests/test_acl.py` (новый), `tests/test_audit.py` (новый), `tests/test_entity_resolver.py` (новый), `tests/test_bitemporal.py` (новый), `tests/test_bm25.py` (новый), `tests/test_hybrid.py` (новый), `tests/test_consolidator.py` (новый), `tests/test_graph_rank.py` (новый)
- **Что делаем:**
  - ACL: тесты permission check для разных ролей
  - Audit: тесты записи и чтения логов
  - Entity: тесты register, resolve, link
  - Bitemporal: тесты versioning, query by time
  - BM25: тесты full-text search
  - Hybrid: тесты fusion с mock данными
  - Consolidator: тесты merge и archive
  - Graph rank: тесты centrality calculation
- **Зависимости:** все предыдущие фазы
- **Время:** 6-8 часов
- **Риск:** Низкий
- **Ожидаемый эффект:** 100% покрытие нового кода

### Шаг 4.2: Integration-тесты

- **Файлы:** `tests/test_integration_new_features.py` (новый)
- **Что делаем:**
  - Тест end-to-end: store → ACL check → search → hybrid → consolidate
  - Тест temporal: store → update → query by time → get history
  - Тест entity: register → link → search by entity
  - Тест audit: операция → проверка лога
- **Зависимости:** шаг 4.1
- **Время:** 3-4 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** Все фичи работают вместе

### Шаг 4.3: Документация — ARCHITECTURE.md

- **Файлы:** `docs/ARCHITECTURE.md`
- **Что делаем:**
  - Добавить секции:
    - Security: ACL, Audit, Entity Registry
    - Temporal: Bitemporal versioning
    - Search: Hybrid search, BM25, Graph rank
    - Consolidation: Merge, Archive
  - Обновить схему архитектуры
  - Добавить ER-диаграмму новых таблиц
- **Зависимости:** все предыдущие фазы
- **Время:** 2-3 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** Документация актуальна

### Шаг 4.4: Обновить README.md

- **Файлы:** `README.md`
- **Что делаем:**
  - Обновить список tools (17 → 24)
  - Добавить описание новых возможностей
  - Обновить Quick Start
- **Зависимости:** все предыдущие фазы
- **Время:** 1 час
- **Риск:** Низкий
- **Ожидаемый эффект:** README актуален

### Шаг 4.5: Обновить промпт Тиши (akame) — финальный

- **Файлы:** `agents/memory-granulator.md` (в проекте akame)
- **Что делаем:**
  - Финальное обновление описания всех 24 tools
  - Обновить workflow грануляции с учётом temporal, entity linking, consolidation
  - Добавить best practices по использованию новых tools
- **Зависимости:** все предыдущие фазы
- **Время:** 1-2 часа
- **Риск:** Низкий
- **Ожидаемый эффект:** Тишь полностью знает все возможности

---

## Сводная таблица

| Фаза | Задач | Время | Сложность | Эффект |
|------|-------|-------|-----------|--------|
| **P0** | Инфраструктура (0.1-0.4) | 2-3 дня | средняя | Сервер готов, CI/CD, миграции |
| **P1** | Безопасность (1.1-1.7) | 4-5 дней | средняя-высокая | ACL, audit, entity registry, 2 tools |
| **P2** | Временны́е (2.1-2.4) | 3-4 дня | средняя | Bitemporal, 2 tools |
| **P3** | Поиск (3.1-3.6) | 5-6 дней | высокая | Hybrid search, BM25, graph rank, 2 tools |
| **P4** | Тестирование (4.1-4.5) | 3-4 дня | средняя | 100% покрытие, документация |
| **Итого** | | **17-22 дня** | | 7 tools, 4 таблицы, 8 модулей |

---

## Критический путь

```
0.1 (сервер) → 0.3 (миграции) → 1.1 (ACL) → 1.3 (интеграция ACL)
                    ↓
              1.2 (audit) → 1.5 (tool ACL)
                    ↓
              1.4 (entity) → 1.6 (tool entity)
                    ↓
              2.1 (bitemporal) → 2.2 (интеграция) → 2.3, 2.4 (tools)
                    ↓
              3.1 (BM25) → 3.3 (hybrid) → 3.4 (интеграция) → 3.5 (tool)
                    ↓
              3.2 (graph rank) ↗
                    ↓
              3.6 (consolidate)
                    ↓
              4.1-4.5 (тесты, доки)
```

**Независимые задачи (параллелить с любыми):**
- 0.2 (CI/CD)
- 0.4 (requirements.txt)
- 1.7 (промпт Тиши — после tools)
- 4.3, 4.4 (документация — в конце)

---

## Порядок PR

| PR | Фаза | Содержание | Время |
|----|------|------------|-------|
| **PR #1** | P0 | Миграции (0.3), requirements (0.4), CI/CD (0.2) | 1 день |
| **PR #2** | P1 | ACL модуль (1.1) + audit (1.2) + entity (1.4) | 2 дня |
| **PR #3** | P1 | Интеграция ACL в service (1.3) + tools (1.5, 1.6) | 2 дня |
| **PR #4** | P2 | Bitemporal модуль (2.1) + интеграция (2.2) + tools (2.3, 2.4) | 3 дня |
| **PR #5** | P3 | BM25 (3.1) + graph rank (3.2) | 2 дня |
| **PR #6** | P3 | Hybrid search (3.3) + интеграция (3.4) + tool (3.5) | 2 дня |
| **PR #7** | P3 | Consolidator (3.6) | 1 день |
| **PR #8** | P4 | Тесты (4.1, 4.2) | 2 дня |
| **PR #9** | P4 | Документация (4.3, 4.4) + промпт Тиши (4.5) | 1 день |

**Важно:** Каждый PR должен быть獨立ным и не ломать предыдущие. Фиче-флаги для постепенного включения.

---

## Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Сервер не расширен вовремя | средняя | критическое | Начать с Docker, CI/CD работает локально |
| ACL блокирует всех пользователей | низкая | критическое | Дефолт = разрешить всё, постепенное включение |
| Bitemporal ломает update() | средняя | высокое | Фиче-флаг `TEMPORAL_ENABLED`, fallback |
| BM25 медленный на больших объёмах | средняя | среднее | GIN индекс, тестирование на 10k+ |
| Hybrid search даёт худшие результаты | средняя | среднее | Фиче-флаг, A/B тестирование |
| Consolidator теряет данные при merge | низкая | критическое | dry_run по умолчанию, backup, ручное подтверждение |
| Migration на проде ломает данные | низкая | критическое | Backup, тестирование на копии, идемпотентные миграции |
| Промпт Тиши не обновлён | низкая | среднее | PR #9 — последний, проверить перед merge |

---

## Зависимости между ролями

| Роль | Задачи |
|------|--------|
| **Нора** | Миграции (0.3), SQL для BM25 (3.1), индексы |
| **Сона** | Все модули (acl, audit, entity, bitemporal, bm25, hybrid, consolidator), все tools |
| **Эна** | Архитектурный контроль, review всех PR |
| **Рэй** | Сервер (0.1), CI/CD (0.2), Docker, деплой |
| **Мая** | Метрики для всех новых модулей, Grafana панели |
| **Катерина** | Тесты (4.1, 4.2), regression testing |
| **Лита** | Security review ACL, audit, RLS |
| **Тиамат** | Документация (4.3, 4.4) |
| **Тишь** | Промпт (1.7, 4.5), интеграция с akame |

---

## Параллелизация

```
День 1-2:  [Рэй: сервер] [Нора: миграции] [Сона: ACL модуль]
День 3-4:  [Сона: audit + entity] [Мая: метрики] [Лита: security review]
День 5-6:  [Сона: интеграция ACL + tools] [Катерина: тесты ACL]
День 7-8:  [Сона: bitemporal] [Нора: temporal индексы]
День 9-10: [Сона: BM25 + graph rank] [Мая: метрики поиска]
День 11-12: [Сона: hybrid search + consolidate] [Катерина: тесты поиска]
День 13-14: [Тиамат: документация] [Тишь: промпт] [Катерина: regression]
День 15:   [Рэй: деплой] [Все: финальный review]
```
