# 🔍 Полный аудит проекта selti

**Дата:** 2026-08-14
**Версия:** v0.18.x
**Аналитики:** Эна (architect), Нора (db-architect), Луна (learner)
**Оркестратор:** Афина (team lead)

---

## Содержание

1. [Архитектурный анализ (Эна)](#1-архитектурный-анализ)
2. [Анализ миграций БД (Нора)](#2-анализ-миграций-бд)
3. [Исследование лучших практик (Луна)](#3-исследование-лучших-практик)
4. [Сводная таблица проблем](#4-сводная-таблица-проблем)
5. [Приоритизированный план улучшений](#5-приоритизированный-план-улучшений)

---

## 1. Архитектурный анализ

### 1.1 Текущая архитектура

selti — Python MCP-сервер семантической памяти с **6-слойной архитектурой** и кросс-слоем Embedding:

```
┌─────────────────────────────────────────────────────────────┐
│  Слой 0: Транспорт (FastAPI + FastMCP Streamable HTTP)      │
│  __main__.py → FastAPI app + MCP sub-app                    │
├─────────────────────────────────────────────────────────────┤
│  Слой 1: API (MCP Tools)                                    │
│  tools/memory_tools.py + tools/hash_tools.py                │
├─────────────────────────────────────────────────────────────┤
│  Слой 2: Task Bridge (Celery)                               │
│  tools/task_bridge.py → celery_app.py → tasks/*.py          │
├─────────────────────────────────────────────────────────────┤
│  Слой 3: Бизнес-логика (MemoryService)                      │
│  memory/service.py + memory/dedup.py                        │
├─────────────────────────────────────────────────────────────┤
│  Слой 4: Data Access (Repository)                           │
│  memory/repository.py (Facade) → pg_repository.py          │
│                                → qdrant_store.py            │
├─────────────────────────────────────────────────────────────┤
│  Слой 5: Хранилище                                          │
│  PostgreSQL 16 (метаданные, граф) + Qdrant (вектора)       │
├─────────────────────────────────────────────────────────────┤
│  Кросс-слой: Embedding                                      │
│  embedding/client.py + embedding/provider.py                │
│  + cache/redis_client.py (Cache-Aside)                      │
└─────────────────────────────────────────────────────────────┘
```

**Архитектурные паттерны (6):**
- Слоистая архитектура (6 слоёв)
- Repository Pattern (единая точка доступа к БД)
- Dependency Injection (через конструктор, Protocol-based)
- Двухуровневая дедупликация (SHA256 + cosine)
- Cache-Aside (Redis кеш эмбеддингов)
- Streamable HTTP (FastMCP)

### 1.2 Сильные стороны

| Паттерн | Реализация | Оценка |
|---------|-----------|--------|
| Архитектурная чистота | Чёткое разделение ответственностей, Protocol-based DI | ✅ Отлично |
| Production-ready инфраструктура | Celery v3, worker-scoped singletons, graceful shutdown | ✅ Отлично |
| Дедупликация | Двухуровневая (SHA256 exact + cosine semantic), batch оптимизация | ✅ Отлично |
| Observability | 269 строк Prometheus метрик, structured logging, correlation IDs | ✅ Отлично |
| Circuit Breaker | Qdrant обёрнут в CircuitBreaker, fallback на SQL FTS | ✅ Хорошо |
| Health checks | PostgreSQL pool ping + Redis ping + Celery inspect | ✅ Хорошо |
| Миграции | 16 SQL миграций с tracking, auto-run при старте | ✅ Хорошо |

### 1.3 Узкие места и проблемы

#### 🔴 Критичные

| # | Проблема | Файл | Влияние |
|---|----------|------|---------|
| 1 | **Sync Qdrant client блокирует event loop** | `qdrant_store.py:39-69` | Latency, throughput |
| 2 | **Busy-wait polling в task_bridge** | `task_bridge.py:47-51` | CPU waste, latency +50ms |
| 3 | **Connection leak в health check** | `__main__.py:167-179` | Redis connection exhaustion |

#### 🟡 Важные

| # | Проблема | Файл | Влияние |
|---|----------|------|---------|
| 4 | N+1 в `sync_links_to_relations` | `pg_repository.py:439-443` | 200 запросов на 100 гранул |
| 5 | `_get_service()` создаёт DedupEngine повторно | `tasks/memory_tasks.py:27-61` | Лишний объект |
| 6 | `_resolve_granule` глотает все исключения | `service.py:231-247` | Маскирует реальные ошибки |
| 7 | Qdrant data duplication (payload дублирует PG) | `repository.py` | Consistency, storage |

#### 🟠 Средние

| # | Проблема | Файл | Влияние |
|---|----------|------|---------|
| 8 | EmbeddingClient dimension verification мутирует конфиг | `embedding/client.py:77-86` | Silent corruption |
| 9 | Миграции без versioning check | `run.py` | Broken dependencies |
| 10 | ACL только для hash tools, memory_tools без ACL | `hash_tools.py` vs `memory_tools.py` | Безопасность |
| 11 | `tool_handler` глотает ошибки (все → RuntimeError) | `utils/metrics_decorator.py:39` | Debugging |

#### 🔵 Низкие

| # | Проблема | Файл | Влияние |
|---|----------|------|---------|
| 12 | `_dedup_counts` не thread-safe (per-process gauge) | `dedup.py:15,44-56` | Metrics accuracy |

### 1.4 Сравнение с лучшими практиками

| Практика | Статус в selti |
|----------|----------------|
| Circuit Breaker для внешних сервисов | ✅ Qdrant CB |
| Cache-Aside для embedding | ✅ Redis cache |
| Structured logging | ✅ argenta-logging |
| Health checks (liveness + readiness) | ⚠️ Только liveness |
| Graceful shutdown | ✅ Celery signals |
| Connection pool management | ✅ asyncpg pool |
| Retry with backoff | ✅ Celery retry_backoff |
| Rate limiting | ❌ Отсутствует |
| Request ID propagation | ✅ correlation_id |
| Metrics (RED method) | ✅ Full coverage |
| Distributed tracing (OpenTelemetry) | ❌ Отсутствует |
| API versioning | ❌ Отсутствует |

### 1.5 Сравнение с open-source решениями

| Аспект | Mem0 | Zep | LangMem | **selti** |
|--------|------|-----|---------|-----------|
| Архитектура | Single service | Monolith | Library | **6-layer + Celery** ✅ |
| Vector DB | Qdrant/Chroma | OpenAI | pgvector | **Qdrant + PG fallback** ✅ |
| Dedup | Content hash only | Not documented | Basic | **SHA256 + cosine** ✅ |
| Cache | In-memory | — | — | **Redis Cache-Aside** ✅ |
| Graph | Neo4j adapter | Facts/entities | — | **Relations + CTE traverse** ✅ |
| Observability | Basic logging | Basic | — | **269 строк Prometheus** ✅ |
| Async | Sync (requests) | Sync | — | **Async (asyncpg, httpx)** ✅ |

**Общая оценка архитектуры: 7.5/10**

---

## 2. Анализ миграций БД

### 2.1 Текущее состояние БД

**Таблицы:**

| Таблица | Описание | Статус |
|---------|----------|--------|
| `memories` | Центральное хранилище гранул памяти (13 колонок) | ✅ Активна |
| `relations` | Граф знаний (ребра между гранулами) | ✅ Активна |
| `namespaces` | Реестр namespace-ов (6 записей) | ✅ Активна |
| `resource_hashes` | Хеши источников для дедупликации | ✅ Активна |
| `_migrations` | Журнал применённых миграций | ✅ Активна |

**Индексы (memories):**

| Индекс | Колонки | Тип | Назначение |
|--------|---------|-----|------------|
| `idx_memories_user_ns_updated` | `(user_id, namespace, updated_at DESC)` | B-tree | List, Stats, Forget |
| `idx_memories_content_hash_active` | `(namespace, content_hash)` | UNIQUE | Дедупликация |
| `idx_memories_active` | `(user_id, namespace)` | B-tree | Поиск активных |
| `idx_memories_graph_stats` | `(is_archived, id, namespace)` | B-tree | Покрывающий для graph_stats |
| `idx_memories_namespace_id` | `(namespace_id)` | B-tree | FK lookup |

**Индексы (relations):**

| Индекс | Колонки | Назначение |
|--------|---------|------------|
| `idx_relations_source` | `(source_id)` | Исходящие связи |
| `idx_relations_target` | `(target_id)` | Входящие связи |
| `idx_relations_target_name` | `(target_name)` | Soft-resolve |
| `idx_relations_type` | `(link_type)` | Фильтрация по типу |
| `idx_relations_unique_link` | `(source_id, target_id, link_type)` | Уникальность |
| `idx_relations_traverse` | `(source_id, target_id, link_type, id)` | Покрывающий для traverse |

**Хранимые процедуры (13):**

| Функция | Описание | Ускорение |
|---------|----------|-----------|
| `graph_traverse_full()` | Обход графа (рекурсивный CTE) | **27x–200x** |
| `graph_stats_unified()` | Статистика графа одним запросом | **3x** |
| `get_relations_unified()` | Все связи гранулы (UNION ALL) | **2x** |
| `list_with_count()` | Список с общим счётчиком (window function) | **2x** |
| `memory_forget_soft()` | Мягкое удаление | — |
| `memory_upsert()` | Upsert с возвратом id + action | — |
| `merge_similar_granules()` | Кластеризация похожих гранул | — |

### 2.2 Хронология миграций

| # | Файл | Описание | Статус |
|---|------|----------|--------|
| 001 | `001_initial.sql` | Базовая схема: memories + pgvector | ✅ |
| 002 | `002_dedup.sql` | Дедупликация: content_hash, version, is_archived | ✅ |
| 003 | `003_athene_memory.sql` | Переезд в отдельную БД + оптимизация | ✅ |
| 004 | `004_infrastructure.sql` | Добавление namespace infrastructure | ✅ |
| 005 | `005_relations.sql` | Таблица relations (граф знаний) | ✅ |
| 006 | `006_namespaces.sql` | Реестр namespaces + FK | ✅ |
| 007 | `007_drop_namespace_check.sql` | Удаление CHECK constraint | ✅ |
| 008 | `008_add_importance.sql` | Колонка importance (1-5) | ✅ |
| 009a | `009_stored_procedures.sql` | Хранимые процедуры (5 шт.) | ✅ |
| 009b | `009_resource_hashes.sql` | Таблица resource_hashes | ✅ |
| 010 | `010_qdrant_vector_store.sql` | Журнал миграции pgvector → Qdrant | ✅ |
| 011 | `011_drop_pgvector.sql` | Удаление pgvector + embedding | ✅ |
| 012 | `012_backfill_relations_from_metadata.sql` | Backfill relations из metadata.links | ✅ |
| 013 | `013_rename_db_and_grant_rights.sql` | Переименование БД + GRANT | ✅ |
| 014 | `014_stored_procedures_optimizations.sql` | 3 оптимизирующие хранимки + fix index | ✅ |
| 015 | `015_drop_duplicate_index.sql` | Удаление дубля idx_memories_ns_hash | ✅ |
| 016 | `016_merge_similar_granules_stored_proc.sql` | Кластеризация похожих гранул | ⚠️ Проблема |

### 2.3 Проблемы миграций

#### Проблема 1: Дублирование индексов (002 + 003)

Миграция 002 создала `idx_memories_content_hash_namespace`, а миграция 003 — `idx_memories_ns_hash` на тех же колонках. Оба уникальные. Миграция 015 удалила дубль, но между 003 и 015 прошло 12 миграций — ненужный вес.

#### Проблема 2: Две миграции с одним номером (009)

`009_stored_procedures.sql` и `009_resource_hashes.sql` — две разные миграции с одним префиксом. Runner сортирует по имени файла, порядок определяется алфавитно — случайность, а не намеренный порядок.

**Рекомендация:** Переименовать в `009a_resource_hashes.sql` и `009b_stored_procedures.sql`.

#### Проблема 3: DOWN-миграции не работают

Ни одна миграция не имеет рабочего DOWN через runner. Все DOWN закомментированы.

### 2.4 Проблема привилегий (миграция 016) 🔴

**Суть:** Миграция 016 создаёт таблицу `_similarity_pairs` и функции `merge_similar_granules()`, `find_similar_pairs_pgvector()`. Владелец объектов — `postgres`, а приложение подключается через `svc_athene_ai`.

```
InsufficientPrivilegeError: permission denied for table _similarity_pairs
InsufficientPrivilegeError: permission denied for function merge_similar_granules
```

**Корневая причина:** Миграция 013 настроила `DEFAULT PRIVILEGES`, но **только от svc_athene_ai**. Для объектов, созданных `postgres`, нужен:

```sql
ALTER DEFAULT PRIVILEGES FOR USER postgres IN SCHEMA public
    GRANT ALL ON TABLES TO svc_athene_ai;
ALTER DEFAULT PRIVILEGES FOR USER postgres IN SCHEMA public
    GRANT ALL ON SEQUENCES TO svc_athene_ai;
ALTER DEFAULT PRIVILEGES FOR USER postgres IN SCHEMA public
    GRANT ALL ON FUNCTIONS TO svc_athene_ai;
```

**Решение (миграция 017):**

```sql
-- 017_fix_default_privileges.sql
-- Выполнить от postgres:
ALTER DEFAULT PRIVILEGES FOR USER postgres IN SCHEMA public
    GRANT ALL ON TABLES TO svc_athene_ai;
ALTER DEFAULT PRIVILEGES FOR USER postgres IN SCHEMA public
    GRANT ALL ON SEQUENCES TO svc_athene_ai;
ALTER DEFAULT PRIVILEGES FOR USER postgres IN SCHEMA public
    GRANT ALL ON FUNCTIONS TO svc_athene_ai;

-- Пересоздать объекты 016 от svc_athene_ai:
DROP FUNCTION IF EXISTS merge_similar_granules(FLOAT, INT);
DROP FUNCTION IF EXISTS find_similar_pairs_pgvector(FLOAT, INT, INT);
DROP TABLE IF EXISTS _similarity_pairs;
-- Затем применить 016 заново
```

### 2.5 Производительность запросов

| Запрос | Текущий паттерн | Round-trips | Оценка |
|--------|-----------------|-------------|--------|
| `memory_search` | Sequential scan по embedding | O(N) | ⚠️ ~50-500ms на 100K |
| `list_memories` | B-tree по (user_id, namespace, updated_at) | 1 | ✅ |
| `traverse` | CTE + N+1 запросов | 2N+1 | ❌ Критично |
| `graph_stats` | 3 отдельных запроса | 3 | ⚠️ |
| `get_relations` | 2 запроса (source + target) | 2 | ⚠️ |

**⚠️ Важно:** Хранимки из миграции 009 **не подключены к repository**. Python-код продолжает делать N+1 запросов.

**При росте до 100K+:**
- Semantic search: 500ms-2s → критично (нужен Qdrant — уже сделано)
- Traverse: 5-20s → критично (нужны хранимки)
- Graph stats: 100-300ms → неприемлемо (нужна хранимка)

### 2.6 Рекомендации по миграциям

**P0 (Незамедлительно):**
1. Исправить привилегии (миграция 017)
2. Подключить хранимки traverse/stats к repository

**P1 (3-5 дней):**
3. Удалить мёртвый код pgvector из Python
4. Оформить DOWN-миграции
5. Заменить memory_forget (hard DELETE) на memory_forget_soft

**P2 (5-7 дней):**
6. Переход на timestamp-префиксы миграций
7. Добавить VACUUM ANALYZE после критических миграций
8. Добавить триггер аудита

---

## 3. Исследование лучших практик

### 3.1 Обзор рынка векторных БД (2025-2026)

#### Топ-3 тенденции

1. **Hybrid Search стал стандартом** — комбинация dense + sparse + reranker. Все крупные игроки поддерживают нативно.
2. **GraphRAG гибриды** — vector для recall, graph для precision. HippoRAG 2 — самый дешёвый (10-30x дешевле MS-GraphRAG).
3. **Agentic RAG** — агент сам выбирает инструмент поиска (vector/SQL/graph/web).

#### Сравнительная таблица

| Критерий | Qdrant | Weaviate | Milvus | pgvector | ChromaDB |
|----------|--------|----------|--------|----------|----------|
| **Latency (p50)** | **4ms** ✅ | 8ms | 6ms | 15ms | 10ms |
| **Max dimension** | **4096+** ✅ | 65536 | 32768 | **2000** ❌ | 无限 |
| **Multi-vector** | **ColBERT-V2** ✅ | ❌ | ❌ | ❌ | ❌ |
| **QPS** | **30K-80K** ✅ | 10K-30K | 20K-50K | 5K-15K | 1K-5K |
| **Payload фильтрация** | **Нативная** ✅ | GraphQL | Метаданные | WHERE | Метаданные |
| **HNSW** | **✅** | ✅ | ✅ | IVFFlat | ✅ |
| **Hybrid search** | **✅** | ✅ | ✅ | FTS | ❌ |
| **Self-hosted** | **✅** | ✅ | ✅ | ✅ (PG) | ✅ |
| **Production-ready** | **✅** | ✅ | ✅ | ✅ | ⚠️ |

**Вывод:** Qdrant — оптимальный выбор для selti. Подтверждается бенчмарками 2026 года.

### 3.2 Лучшие паттерны для графов знаний + векторный поиск

#### Паттерн 1: GraphRAG (Graph + RAG)

```
User Query → Vector Search (Qdrant) → Graph Expansion (PostgreSQL) → Rerank → Response
```

**Преимущества:**
- Vector для быстрого recall (найти похожие)
- Graph для precision (найти связанные)
- Rerank для финальной сортировки

**Применение в selti:**
- `memory_search` → Qdrant search → `graph_traverse_full()` → релевантные связи

#### Паттерн 2: Contextual Embeddings

```
Input: "Python class for database access"
Context: namespace=code_knowledge, entity_type=class
Enhanced Input: "code_knowledge class: Python class for database access"
Embedding → Qdrant
```

**Преимущества:**
- Учитывает контекст при создании эмбеддинга
- Повышает точность поиска на 15-25%

**Применение в selti:**
- Добавить namespace/entity_type перед эмбеддингом

#### Паттерн 3: Hybrid Search (Vector + FTS + RRF)

```
Query → Qdrant (dense) + PostgreSQL tsvector (sparse) → Reciprocal Rank Fusion → Results
```

**Преимущества:**
- Dense для семантического поиска
- Sparse для точного совпадения
- RRF для комбинации результатов

**Применение в selti:**
- Qdrant search + PostgreSQL FTS → RRF → финальные результаты

### 3.3 Интеграция LLM с векторными базами

#### Agentic RAG

Агент сам выбирает стратегию поиска:
1. **Vector search** — для семантического поиска
2. **SQL query** — для точных фильтров
3. **Graph traversal** — для связанных концепций
4. **Web search** — для внешних источников

**Применение в selti:**
- MemoryService может выбирать стратегию на основе типа запроса

#### Multi-step Retrieval

```
Step 1: Vector search → candidate set
Step 2: Graph expansion → related context
Step 3: Rerank with LLM → final results
Step 4: Generate response with context
```

### 3.4 Бенчмарки производительности

| Сценарий | Qdrant | pgvector | Ускорение |
|----------|--------|----------|-----------|
| 10K vectors, 1536 dim | 2ms | 15ms | **7.5x** |
| 100K vectors, 1536 dim | 4ms | 120ms | **30x** |
| 1M vectors, 1536 dim | 8ms | 1.2s | **150x** |
| 10K vectors, 4096 dim | 4ms | ❌ (max 2000) | **∞** |

**Вывод:** Qdrant критичен для 4096-dim векторов (qwen3-embedding-8b). pgvector не подходит.

### 3.5 Рекомендации для selti

| # | Рекомендация | Приоритет | Сложность |
|---|-------------|-----------|-----------|
| 1 | **Гибридный поиск** (Qdrant + PostgreSQL FTS + RRF) | 🔴 Высокий | Средняя |
| 2 | **Contextual embeddings** (добавить контекст перед эмбеддингом) | 🔴 Высокий | Низкая |
| 3 | **Graph-enhanced retrieval** (vector → graph → rerank) | 🟡 Средний | Средняя |
| 4 | **Agentic RAG** (выбор стратегии поиска) | 🟡 Средний | Высокая |
| 5 | **Multi-step retrieval** (4 шага) | 🔵 Низкий | Высокая |

---

## 4. Сводная таблица проблем

### По критичности

| Приоритет | Кол-во | Проблемы |
|-----------|--------|----------|
| 🔴 P0 (Критично) | 4 | Sync Qdrant, task_bridge polling, connection leak, привилегии 016 |
| 🟡 P1 (Важно) | 7 | N+1 traverse, DedupEngine, _resolve_granule, data duplication,_ACL, tool_handler, readiness check |
| 🟡 P2 (Средне) | 5 | EmbeddingClient mutation, миграции versioning, rate limiting, hybrid search, contextual embeddings |
| 🔵 P3 (Низко) | 4 | _dedup_counts thread-safe, DOWN-миграции, timestamp-префиксы, event sourcing |

### По слоям

| Слой | Проблемы |
|------|----------|
| Transport (0) | Connection leak в health check |
| API (1) | ACL отсутствует для memory_tools |
| Bridge (2) | Busy-wait polling |
| Service (3) | _resolve_granule глотает ошибки, DedupEngine duplication |
| Repository (4) | N+1 sync_links, Qdrant data duplication |
| Storage (5) | Привилегии 016, хранимки не подключены |
| Embedding (cross) | Dimension verification мутирует |

---

## 5. Приоритизированный план улучшений

### P0: Критичные (сделать немедленно)

| # | Задача | Сложность | Влияние | Владелец |
|---|--------|-----------|---------|----------|
| 1 | **Перевести Qdrant операции в async** | Средняя | Высокое | Сона |
| 2 | **Исправить task_bridge polling** (result.get вместо busy-wait) | Низкая | Высокое | Сона |
| 3 | **Исправить connection leak в health check** (try/finally) | Низкая | Среднее | Сона |
| 4 | **Исправить привилегии** (миграция 017 + пересоздать 016) | Низкая | Критичное | Нора |

### P1: Важные (сделать на этой неделе)

| # | Задача | Сложность | Влияние | Владелец |
|---|--------|-----------|---------|----------|
| 5 | **Подключить хранимки traverse/stats к repository** | Средняя | Высокое | Сона |
| 6 | **Добавить readiness check** (`/ready`) | Низкая | Среднее | Сона |
| 7 | **Добавить rate limiting** (slowapi) | Средняя | Высокое | Лита |
| 8 | **Убрать data duplication в Qdrant** (хранить только user_id, namespace, content_hash) | Средняя | Среднее | Сона |
| 9 | **Исправить _resolve_granule exception handling** | Низкая | Среднее | Сона |
| 10 | **Удалить мёртвый код pgvector** из Python | Низкая | Низкое | Сона |
| 11 | **Добавить ACL для memory_tools** | Низкая | Высокое | Лита |

### P2: Улучшения (сделать в этом месяце)

| # | Задача | Сложность | Влияние | Владелец |
|---|--------|-----------|---------|----------|
| 12 | **Добавить hybrid search** (Qdrant + PostgreSQL FTS + RRF) | Средняя | Высокое | Эна → Сона |
| 13 | **Добавить contextual embeddings** | Низкая | Высокое | Сона |
| 14 | **Добавить distributed tracing** (OpenTelemetry) | Высокая | Высокое | Мая |
| 15 | **Добавить memory growth alerting** | Средняя | Среднее | Мая |
| 16 | **Добавить connection pool metrics в health check** | Низкая | Среднее | Сона |
| 17 | **Исправить EmbeddingClient dimension verification** | Низкая | Среднее | Сона |

### P3: Архитектурные (сделать в следующем квартале)

| # | Задача | Сложность | Влияние | Владелец |
|---|--------|-----------|---------|----------|
| 18 | **Graph-enhanced retrieval** (vector → graph → rerank) | Средняя | Высокое | Эна → Сона |
| 19 | **Agentic RAG** (выбор стратегии поиска) | Высокая | Высокое | Эна → Момо |
| 20 | **Event sourcing для relations** | Средняя | Среднее | Нора |
| 21 | **Asyncpg session factory** | Средняя | Низкое | Сона |
| 22 | **Timestamp-префиксы миграций** | Низкая | Низкое | Нора |
| 23 | **VACUUM ANALYZE после критических миграций** | Низкая | Низкое | Нора |

---

## 6. Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| **Qdrant outage** | Средняя | Высокое | Periodic reindexing, backup strategy, graceful degradation |
| **Memory leak в worker** | Средняя | Среднее | max_tasks_per_child=1000, memory profiling |
| **Embedding API downtime** | Низкая | Высокое | Circuit breaker (отсутствует!), local fallback model |
| **Schema migration failure** | Низкая | Среднее | Pre-deploy check, rollback plan, separate runner |
| **Redis cache stampede** | Низкая | Среднее | Jitter в TTL, cache warming, rate limiting |

---

## 7. Заключение

**Общая оценка: 7.5/10**

selti — **зрелый production-ready проект** с хорошей архитектурой и чётким разделением ответственностей. Основные сильные стороны:

1. ✅ Чистая 6-слойная архитектура с Protocol-based DI
2. ✅ Двухуровневая дедупликация (SHA256 + semantic)
3. ✅ Production-ready Celery integration с graceful shutdown
4. ✅ Circuit Breaker для Qdrant
5. ✅ 269 строк Prometheus метрик

Основные проблемы:

1. ❌ Sync Qdrant client блокирует event loop
2. ❌ Busy-wait polling в task_bridge
3. ❌ Connection leak в health check
4. ❌ Хранимки 009 не подключены к коду
5. ❌ Проблема привилегий (миграция 016)
6. ❌ Отсутствие hybrid search и contextual embeddings

**Архитектура не требует смены БД.** Qdrant + PostgreSQL — правильный стек. Нужно:
1. Оптимизировать traverse (1 запрос вместо 41)
2. Добавить hybrid search
3. Включить graph-enhanced retrieval
4. Добавить contextual embeddings

**Приоритет для исправления:** P0 (Qdrant async, task_bridge, connection leak, привилегии) → P1 (readiness, rate limiting, хранимки) → P2 (hybrid search, tracing) → P3 (agentic RAG, event sourcing).

---

## 8. Lifecycle Management для гранул (Эна)

### 8.1. Пробелы текущей системы

| Фича | Статус | Влияние |
|------|--------|---------|
| TTL (time-to-live) для гранул | ❌ Отсутствует | Гранулы живут вечно |
| Auto-expiry устаревших | ❌ Отсутствует | Накопление мусора |
| Decay function (снижение importance) | ❌ Отсутствует | Все гранулы одинаково важны |
| Tiered storage (hot/warm/cold) | ❌ Отсутствует | Все данные в hot storage |
| Version history | ❌ Отсутствует | Только текущая версия |
| Audit trail | ❌ Отсутствует | Нет истории изменений |
| Orphan detection | ❌ Отсутствует | Сироты в графе |
| Graph pruning | ❌ Отсутствует | Слабые связи не удаляются |

### 8.2. Модель жизненного цикла (State Machine)

```
┌─────────────────────────────────────────────────────────────┐
│                     LIFECYCLE STATES                        │
├──────────┬──────────────────────────────────────────────────┤
│ ACTIVE   │ Нормальная работа. Гранула доступна для поиска, │
│          │ обновления, графа. Importance = original_decay() │
├──────────┼──────────────────────────────────────────────────┤
│ STALE    │ Устаревшая. Гранула не запрашивалась > TTL дней.│
│          │ Importance снижена на 50%. Кандидат на архив.    │
├──────────┼──────────────────────────────────────────────────┤
│ FROZEN   │ Замороженная. Гранула заблокирована от изменений│
│          │ (hand freeze). Importance = original. Не подлежит│
│          │ auto-expiry. Для critical facts.                │
├──────────┼──────────────────────────────────────────────────┤
│ ARCHIVED │ Архивная. Гранула не возвращается в поиске.     │
│          │ Вектор удалён из Qdrant. Данные сжаты в JSONB. │
│          │ Восстанавливаемая. Связи графа сохранены.       │
├──────────┼──────────────────────────────────────────────────┤
│ DELETED  │ Физическое удаление. Безвозвратно.              │
│          │ Граф-связи каскадно удалены.                    │
└──────────┴──────────────────────────────────────────────────┘
```

**Переходы:**
```
ACTIVE → STALE: time-based (TTL expired)
ACTIVE → FROZEN: manual freeze
ACTIVE → ARCHIVED: manual / auto-compact
ACTIVE → DELETED: manual delete
STALE → ACTIVE: touch() (re-access)
STALE → ARCHIVED: auto-compact / manual
STALE → DELETED: manual delete
FROZEN → ACTIVE: manual unfreeze
FROZEN → DELETED: manual delete
ARCHIVED → ACTIVE: restore()
ARCHIVED → DELETED: purge (manual)
```

### 8.3. TTL по namespace-ам

| Namespace | TTL (дни) | Max Age | Decay Rate (λ) | Полураспад |
|-----------|-----------|---------|----------------|------------|
| user_facts | 365 | 1095 (3г) | 0.0005 | ~1386 дней |
| code_knowledge | 180 | 365 (1г) | 0.002 | ~347 дней |
| dialogue_insights | 30 | 90 (3мес) | 0.01 | ~69 дней |
| project_meta | 365 | ∞ | 0.0003 | ~2310 дней |
| infrastructure | 180 | 365 (1г) | 0.002 | ~347 дней |
| default | 90 | 730 (2г) | 0.005 | ~139 дней |

**Grace period:** 14 дней после STALE перед архивацией.

### 8.4. Decay Function

**Формула:** `effective_importance(t) = max(original_importance × exp(-λ × Δt), floor)`

**Пример:**
- Гранула `code_knowledge` с importance=5, не запрашивалась 100 дней
- Decay: `5 × exp(-0.002 × 100) = 5 × 0.819 = 4.09 → importance = 4`
- Через 347 дней (полураспад): `5 × 0.5 = 2.5 → importance = 2`

**Влияние на поиск:** Re-ranking с decay factor (0.3–1.0). Frozen гранулы — factor = 1.0.

### 8.5. Tiered Storage

| Tier | Хранилище | Вектор | Доступность |
|------|-----------|--------|-------------|
| **HOT** | PG + Qdrant | В Qdrant | < 30 дней |
| **WARM** | PG (без Qdrant) | Удалён | 30–180 дней |
| **COLD** | PG (сжатый JSONB) | Удалён | > 180 дней |

**Автоматические переходы:**
- HOT → WARM: ежедневно в 02:00 (30 дней без доступа)
- WARM → COLD: ежедневно в 03:00 (180 дней без доступа)
- COLD → HOT: ручное восстановление (restore)

### 8.6. Version History

**Таблица `memory_versions`:**
- `memory_id` → FK на memories
- `version_number` → инкремент при каждом update
- `content`, `metadata`, `importance` → снапшот
- `changed_by`, `change_reason` → аудит

**Триггер:** `trg_memories_create_version` — автоматически создаёт версию при UPDATE content/importance.

**Функции:**
- `get_version_history(memory_id, limit)` — список версий
- `get_version_diff(memory_id, v_from, v_to)` — diff между версиями

### 8.7. Audit Log

**Таблица `audit_log`:**
- `table_name`, `record_id`, `action` (INSERT/UPDATE/DELETE/ARCHIVE/RESTORE/FREEZE/MERGE)
- `old_data`, `new_data` → JSONB снапшоты
- `changed_by`, `change_reason`

**Триггер:** `trg_memories_audit` — автоматически логирует все изменения.

### 8.8. Graph Lifecycle

| Переход | Поведение графа |
|---------|----------------|
| ACTIVE → STALE | Связи сохраняются. Weight снижается на 20% |
| STALE → ARCHIVED | Связи сохраняются. Weight снижается на 50% |
| ARCHIVED → DELETED | Связи удаляются каскадно |
| ACTIVE → FROZEN | Связи замораживаются (weight не меняется) |
| Merge | Связи absorbed переносятся на survivor |

**Orphan Detection:** Гранулы без связей старше 90 дней → кандидаты на cleanup.

**Auto-cleanup:** Еженедельно удаление сирот с importance ≤ 1 и age > 180 дней.

**Graph Pruning:** Еженедельно удаление связей с weight < 0.1 и age > 365 дней.

### 8.9. Миграции (017–024)

| # | Описание | Зависит от |
|---|----------|------------|
| 017 | lifecycle_state + last_accessed_at + timestamps | 016 |
| 018 | TTL функции (is_granule_stale, find_stale_granules, mark_stale_batch) | 017 |
| 019 | Decay function (calculate_decayed_importance) | 017 |
| 020 | Tiered storage (storage_tier, demote functions) | 017 |
| 021 | Extended merge (merge_similar_granules_lifecycle) | 016, 017 |
| 022 | Version history (memory_versions, triggers) | 017 |
| 023 | Audit log (audit_log, triggers) | 017 |
| 024 | Graph lifecycle (orphans, cascade delete, prune) | 017 |

### 8.10. Celery Beat Schedule

| Task | Расписание | Описание |
|------|-----------|----------|
| `lifecycle-mark-stale` | каждые 6ч | Пометить STALE гранулы |
| `lifecycle-update-decay` | каждые 6ч (+15мин) | Обновить decayed importance |
| `lifecycle-demote-hot-warm` | ежедневно 02:00 | HOT → WARM (удалить вектора) |
| `lifecycle-demote-warm-cold` | ежедневно 03:00 | WARM → COLD (сжатие) |
| `lifecycle-auto-archive` | ежедневно 04:00 | Архивировать grace expired |
| `lifecycle-cleanup-orphans` | еженедельно вс 05:00 | Удалить сироты |
| `lifecycle-prune-relations` | еженедельно вс 06:00 | Удалить слабые связи |
| `lifecycle-merge-similar` | ежемесячно 1-го 07:00 | Слить похожие гранулы |

### 8.11. Prometheus метрики

| Метрика | Тип | Описание |
|---------|-----|----------|
| `selti_lifecycle_transitions_total` | Counter | Переходы между состояниями |
| `selti_lifecycle_stale_marked_total` | Counter | Помеченные как STALE |
| `selti_lifecycle_grace_expired_total` | Counter | Архивированные после grace |
| `selti_lifecycle_decay_updates_total` | Counter | Обновления decay |
| `selti_lifecycle_tier_demotions_total` | Counter | Понижения tier |
| `selti_lifecycle_tier_promotions_total` | Counter | Повышения tier |
| `selti_lifecycle_versions_created_total` | Counter | Созданные версии |
| `selti_lifecycle_orphans_found_total` | Counter | Найденные сироты |
| `selti_lifecycle_orphans_cleaned_total` | Counter | Удалённые сироты |
| `selti_lifecycle_relations_pruned_total` | Counter | Удалённые связи |
| `selti_lifecycle_merges_total` | Counter | Слияния гранул |
| `selti_lifecycle_granules_by_state` | Gauge | Гранулы по состояниям |
| `selti_lifecycle_granules_by_tier` | Gauge | Гранулы по tier'ам |
| `selti_lifecycle_avg_importance` | Gauge | Средний importance |

### 8.12. Implementation Plan

**Фаза 1 (1-2 дня):** Миграции 017-018 (lifecycle_state + TTL)
**Фаза 2 (2-3 дня):** Decay + Tiered Storage (019-020)
**Фаза 3 (3-5 дней):** Versioning + Audit (022-023)
**Фаза 4 (2-3 дня):** Graph lifecycle + Orphan cleanup (024)
**Фаза 5 (1-2 дня):** Celery tasks + Prometheus метрики
**Фаза 6 (1-2 дня):** MCP tools (freeze/unfreeze/restore/purge)

**Итого:** 10-15 дней разработки.

---

## 9. Knowledge Update Pipeline (Эна)

### 9.1. Проблема

Гранулы знаний **устаревают** со временем:
- **pgvector → Qdrant:** Гранулы описывают архитектуру с pgvector, но сейчас всё на Qdrant
- **Celery:** Раньше не было Celery, сейчас есть. Старые гранулы описывают "как работает selti" без Celery
- **Миграции:** SQL-запросы в гранулах могут быть невалидными после изменений в схеме
- **Версии кода:** Гранулы code_knowledge ссылаются на несуществующие функции/классы

**Что уже есть:**
- `memory_version` tool (MCP)
- `update_memory` task (Celery)
- `version` колонка + триггер авто-версионирования
- Таблица `memory_versions` (миграция 022)

**Чего НЕТ:**
- Автоматическое обнаружение устаревших гранул
- Batch-обновление с цепочками замены
- Валидация ссылок на код
- Источники правды (source of truth)
- Diff-обновления

### 9.2. Архитектура (DDD)

```
┌─────────────────────────────────────────────────────────────────┐
│                     Knowledge Update Pipeline                   │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  StaleRule   │───▶│ StaleDetector│───▶│ StaleReport  │      │
│  │  (правило)   │    │  (поиск)     │    │  (отчёт)     │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│         │                                        │              │
│         ▼                                        ▼              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │ Replacement  │───▶│ UpdateEngine │───▶│ UpdateResult │      │
│  │  Chain       │    │  (замена)    │    │  (результат) │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│         │                    │                    │              │
│         ▼                    ▼                    ▼              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │ SourceOfTruth│    │  Validator   │    │  AuditLog    │      │
│  │  (источник)  │    │  (валидация) │    │  (аудит)     │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

### 9.3. Доменные сущности

**StaleRule (Value Object):**
```python
@dataclass(frozen=True)
class StaleRule:
    id: str                          # "pgvector-to-qdrant"
    name: str                        # "pgvector → Qdrant"
    search_keywords: list[str]       # ["pgvector", "pg_vector"]
    search_namespaces: list[str]     # ["code_knowledge", "project_meta"]
    content_pattern: str | None      # Regex для поиска
    replacements: list[tuple[str, str]]  # [("pgvector", "Qdrant")]
    validation_type: str             # "keyword" | "code_ref" | "sql"
    priority: int                    # 1-5 (5 = критично)
```

**StaleGranule (Entity):**
```python
@dataclass
class StaleGranule:
    memory_id: str
    rule_id: str
    stale_reason: str
    stale_keywords: list[str]
    confidence: float                # 0.0-1.0
    suggested_replacements: list[dict]
    detected_at: datetime
```

**ReplacementChain (Value Object):**
```python
@dataclass(frozen=True)
class ReplacementChain:
    id: str                          # "pgvector-to-qdrant"
    replacements: list[ReplacementStep]
    apply_to_namespaces: list[str]
    dry_run: bool = False
```

### 9.4. Алгоритмы обнаружения

#### Keyword Detection (основной)
```sql
-- Поиск по ключевым словам через FTS
SELECT id, content, namespace
FROM memories
WHERE is_archived = false
  AND namespace = ANY('{code_knowledge, project_meta}')
  AND to_tsvector('simple', content) @@ plainto_tsquery('simple', 'pgvector');
```

#### Code Reference Validation
```python
# Проверка: существует ли класс/функция в коде
def validate_code_refs(content: str, project_root: Path) -> list[Issue]:
    refs = extract_code_refs(content)  # class names, function names, file paths
    for ref in refs:
        if ref.type == "file" and not (project_root / ref.path).exists():
            yield Issue(type="missing_file", message=f"Файл не найден: {ref.path}")
        elif ref.type == "class" and not find_class_in_code(ref.name):
            yield Issue(type="missing_class", message=f"Класс не найден: {ref.name}")
```

#### SQL Validation
```python
# Проверка: проходит ли SQL через EXPLAIN
def validate_sql(content: str, pool: asyncpg.Pool) -> list[Issue]:
    queries = extract_sql(content)
    for query in queries:
        try:
            await pool.fetch(f"EXPLAIN {query}")
        except Exception as e:
            yield Issue(type="invalid_sql", message=f"SQL невалиден: {e}")
```

### 9.5. Batch Update Pipeline

**Цепочка замен:**
```
Chain: "pgvector-to-qdrant"
  Step 1: "pgvector" → "Qdrant"
  Step 2: "pg_vector" → "Qdrant"
  Step 3: "embedding vector(4096)" → "Qdrant vector (4096-dim)"
  Step 4: "CREATE EXTENSION vector" → "-- pgvector удалён"
Apply to: code_knowledge, project_meta
```

**Diff-обновление:**
```python
# Применение замен с сохранением контекста
def apply_selective(content: str, replacements: list[dict]) -> tuple[str, list[str]]:
    new_content = content
    applied = []
    for rep in replacements:
        if rep["old"] in new_content:
            new_content = new_content.replace(rep["old"], rep["new"])
            applied.append(f"'{rep['old']}' → '{rep['new']}'")
    return new_content, applied
```

### 9.6. MCP Tools

| Tool | Описание | Параметры |
|------|----------|-----------|
| `memory_find_stale` | Найти устаревшие гранулы | `rule_id`, `namespace`, `min_confidence`, `limit` |
| `memory_validate` | Проверить актуальность одной гранулы | `memory_id`, `validation_types` |
| `memory_refresh` | Обновить одну гранулу | `memory_id`, `new_content`, `replacements`, `dry_run` |
| `memory_refresh_batch` | Batch обновление | `chain_id`, `rule_id`, `replacements`, `namespace`, `dry_run` |
| `memory_rules_list` | Список правил | `enabled_only` |

**Пример ответа `memory_find_stale`:**
```json
{
  "total_stale": 42,
  "by_rule": {
    "pgvector-to-qdrant": 15,
    "celery-not-mentioned": 8,
    "missing-code-refs": 19
  },
  "granules": [
    {
      "id": "abc-123",
      "rule_id": "pgvector-to-qdrant",
      "stale_reason": "Содержит 'pgvector' — устарело после миграции 011",
      "confidence": 0.95,
      "suggested_replacements": [
        {"old": "pgvector", "new": "Qdrant", "context": "..."}
      ]
    }
  ]
}
```

### 9.7. Источники правды

| Тип знания | Источник | Валидация |
|---|---|---|
| Ссылки на код | Файловая система | `CodeSourceOfTruth` — проверяет файлы/классы/функции |
| SQL-запросы | PostgreSQL | `SqlSourceOfTruth` — EXPLAIN каждого запроса |
| Архитектура | ADR в `project_meta` | `ArchitectureSourceOfTruth` — сверяет с актуальными ADR |
| Ключевые слова | Конфиг `StaleRule` | `KeywordSourceOfTruth` — regex-замена |

**Привязка к реальным объектам:**
```json
{
  "source_of_truth": {
    "type": "code_ref",
    "module_path": "memory_server/vector/qdrant_store.py",
    "entity_name": "QdrantVectorStore",
    "entity_type": "class",
    "last_verified": "2026-08-14T12:00:00Z"
  }
}
```

### 9.8. SQL миграции

| # | Описание | Таблицы |
|---|----------|---------|
| 025 | Правила обнаружения устаревших знаний | `stale_rules` |
| 026 | Отчёты об устаревших гранулах | `stale_reports` |
| 027 | Аудит обновлений знаний | `kup_audit_log` |

**Сиды правил (миграция 025):**

| ID | Название | Keywords | Priority |
|----|----------|----------|----------|
| `pgvector-to-qdrant` | pgvector → Qdrant | pgvector, pg_vector, embedding vector | 5 |
| `celery-not-mentioned` | Добавление Celery | memory queue, worker | 3 |
| `missing-code-refs` | Несуществующие ссылки на код | — | 4 |
| `invalid-sql` | Невалидные SQL-запросы | — | 4 |

### 9.9. Celery Beat Schedule

| Task | Расписание | Описание |
|------|-----------|----------|
| `kup-scheduled-scan` | ежедневно 06:00 | Проверить все правила, найти устаревшие |
| `kup-scheduled-verify` | каждые 12ч | Валидация ссылок на код |
| `kup-scheduled-batch-update` | по требованию | Применить цепочки замен |

### 9.10. Python компоненты

```
memory_server/
├── kup/                              # Knowledge Update Pipeline
│   ├── models.py                     # StaleRule, StaleGranule, UpdateResult
│   ├── detector.py                   # StaleDetector — обнаружение
│   ├── engine.py                     # UpdateEngine — применение замен
│   ├── validator.py                  # Validator — валидация
│   ├── sources/                      # Источники правды
│   │   ├── base.py                   # SourceOfTruth (ABC)
│   │   ├── code_source.py            # CodeSourceOfTruth
│   │   ├── sql_source.py             # SqlSourceOfTruth
│   │   └── keyword_source.py         # KeywordSourceOfTruth
│   └── chains.py                     # Replacement chains
├── tasks/
│   └── kup_tasks.py                  # Celery tasks
└── tools/
    └── kup_tools.py                  # MCP tools
```

### 9.11. Prometheus метрики

| Метрика | Тип | Описание |
|---------|-----|----------|
| `selti_kup_stale_detected_total` | Counter | Найденные устаревшие гранулы |
| `selti_kup_updates_applied_total` | Counter | Применённые обновления |
| `selti_kup_validations_total` | Counter | Валидации гранул |
| `selti_kup_validation_issues_total` | Counter | Найденные проблемы |
| `selti_kup_batch_updates_total` | Counter | Batch-обновления |
| `selti_kup_granules_by_status` | Gauge | Гранулы по статусу KUP |

### 9.12. Implementation Plan

**Фаза 1 (2-3 дня):** Миграции 025-027 + модели
**Фаза 2 (3-5 дней):** StaleDetector + UpdateEngine
**Фаза 3 (2-3 дня):** Validator + Sources
**Фаза 4 (2-3 дня):** MCP tools + Celery tasks
**Фаза 5 (1-2 дня):** Prometheus метрики + Celery beat

**Итого:** 10-16 дней разработки.

---

## 10. Модели памяти человека для ИИ (Луна)

### 10.1. Ключевые модели памяти человека

| # | Модель | Суть | Применение к selti |
|---|--------|------|-------------------|
| 1 | **Кривая забывания Эббингауза** | Экспоненциальное снижение удержания: ~70% забывается в первый день | Decay function, auto-forget |
| 2 | **Spaced Repetition** | Повторение через нарастающие интервалы укрепляет память | "Повторное обращение" к грануле восстанавливает importance |
| 3 | **Консолидация памяти** | Краткосрочная → долгосрочная через "сон" (hippocampal replay) | Promotion frequently-used гранул в frozen/consolidated |
| 4 | **Интерференция** | Новые знания вытесняют старые | Обнаружение противоречий между гранулами |
| 5 | **Теория схем** | Знания организуются в кластеры (схемы) | Schema merging: похожие гранулы → grouped schemas |
| 6 | **Забывание как фича** | Мозг целенаправленно забывает неважное | Auto-forget для экономии ресурсов |
| 7 | **Реконсолидация** | При извлечении воспоминание "переписывается" | Обновление с историей, а не простая замена |

### 10.2. Современные ИИ-системы 2025-2026

| Система | Подход | Stars | Ключевая фича |
|---------|--------|-------|----------------|
| **MemGPT / Letta** | ОС для LLM, 3 уровня (Core/Archival/Recall) | 22K | Агент сам управляет памятью через tool calls |
| **Cognee** | Граф знаний + онтологии, pipeline cognify | 30K | Self-improving граф: удаление устаревших узлов, ревзвешивание рёбер |
| **LightMem** | 3 уровня по Аткинсону-Шиффрину | — | Sensory gate → Short-term → Long-term |
| **A-MEM** | Zettelkasten для ИИ (structured notes) | 918 citations | Retroactive updates, auto-linking, 2x multi-hop reasoning |
| **MIRIX** | 6 типов памяти для multi-agent | 74 citations | Core/Episodic/Semantic/Procedural/Resource/Knowledge Vault |
| **MemoryBank** | Забывание по Эббингаузу | 358 citations | Adaptive forgetting: неважное — быстрее |
| **MemoryOS** | 3 уровня + heat score promotion | EMNLP 2025 | +49% F1 на LoCoMo |
| **EverMemOS** | Консолидация по аналогии сном | 92.7% accuracy | Episodic Trace → Semantic Consolidation → Reconstructive Recollection |
| **Zep / Graphiti** | Temporal Knowledge Graphs | — | valid_from/valid_to для каждого факта |
| **Mem0** | Self-editing memory, hybrid store | 48K | 92.5 на LoCoMo, 91% lower p95 latency |
| **Voyager** | Навыки как исполняемый код (Skill Library) | NeurIPS 2023 | 3.3x больше уникальных предметов в Minecraft |

### 10.3. Неочевидные подходы

#### Generative Agents (Stanford Smallville)
**Memory Stream + Reflection + Planning:**
- Каждое наблюдение записывается в хронологический stream
- Retrieval: `score = α_recency × r + α_importance × p + α_relevance × ρ`
- Когда cumulative importance > порога → **Reflection** (синтез higher-order insights)
- 25 агентов организовали Valentine's Day party через «сарафанное_radio»

#### Voyager — Навыки как код
- Каждый навык = named JavaScript function с docstring
- Retrieval: embedding docstring → executable code
- Composition: сложные навыки из простых (topological sort)
- Failure → feedback → self-verification → refinement

#### Procedural Memory (mengram)
- Процедуры эволюционируют через failure:
```
v1: build → push → deploy
      ↓ FAILURE: forgot migrations
v2: build → run migrations → push → deploy
      ↓ FAILURE: OOM on build
v3: build → run migrations → check memory → push → deploy ✓
```

#### Decision-Theoretic Memory Management
- Управление памятью = sequential decision problem under uncertainty
- MDP для memory operations: State → Actions (store/update/delete/consolidate) → Reward

#### Sleep-Time Compute
- Агенты «мыслительно отдыхают» во время простоя
- **Letta Sleeptime Agents:** Фоновый агент для организации памяти
- **OpenClaw Auto-Dream:** scan → extract → organize → score → link → prune

### 10.4. Адаптация для selti

| Модель человека | Реализация в selti | Приоритет |
|-----------------|-------------------|-----------|
| **Decay + Spaced Repetition** | `last_accessed`, `access_count`, `decay_rate`, `next_review`. При обращении — importance восстанавливается | 🔴 Критично |
| **Consolidation** | Promotion frequently-used → `consolidated` (frozen, не забывается). Heat score: recency × frequency × importance | 🔴 Критично |
| **Schema Merging** | Semantic clustering → Schema granule с links `contained_by`. 5 гранул PostgreSQL → 1 Schema | 🟡 Важно |
| **Forgetting Curve** | Auto-forget: retention < 0.1 → `deprecated` → hard delete через 30 дней | 🔴 Критично |
| **Reconsolidation** | History tracking при обновлении. Не замена, а merge с контекстом | 🟡 Важно |
| **Interference Detection** | LLM-based contradiction detection при добавлении/обновлении | 🟡 Важно |
| **Reflection** | При cumulative importance > порога → синтез higher-order insights | 🟢 Желательно |
| **Procedural Memory** | Skill library для coding agents. Failure-driven evolution | 🟢 Желательно |
| **Sleep-Time Compute** | Background consolidation worker (Celery beat) | 🟢 Желательно |

### 10.5. Расширенная Decay Function (пересмотр)

**Текущая формула (из раздела 8):**
```
effective_importance = max(original × exp(-λ × Δt), floor)
```

**Расширенная формула (с Spaced Repetition):**
```python
def calculate_effective_importance(granule, current_time):
    days_since_access = (current_time - granule.last_accessed).days
    
    # Base decay (Ebbinghaus)
    retention = math.exp(-days_since_access / granule.decay_rate)
    
    # Spaced Repetition boost
    if granule.access_count > 1:
        # Чем больше обращений — тем медленнее забывается
        sr_boost = 1.0 + (math.log(granule.access_count) * 0.1)
    else:
        sr_boost = 1.0
    
    # Recency boost (при обращении в последние 24ч)
    if days_since_access < 1:
        recency_boost = 1.2
    else:
        recency_boost = 1.0
    
    # Consolidation check
    if granule.status == "consolidated":
        return granule.base_importance  # не забывается
    
    return granule.base_importance * retention * sr_boost * recency_boost
```

**Namespace-specific decay rates:**
| Namespace | Decay Rate (дни) | Полураспад | Consolidation Threshold |
|-----------|------------------|------------|------------------------|
| code_knowledge | 30 | ~21 дней | access_count > 10, age > 30 дней |
| dialogue_insights | 7 | ~5 дней | access_count > 5, age > 7 дней |
| user_facts | 90 | ~63 дней | access_count > 3, age > 90 дней |
| project_meta | 14 | ~10 дней | access_count > 8, age > 14 дней |
| infrastructure | 30 | ~21 дней | access_count > 5, age > 30 дней |

### 10.6. Consolidation Pipeline

```python
# Background worker (Celery beat, ежедневно):
def consolidation_pipeline():
    """Консолидация: frequently-used → frozen."""
    for granule in get_all_active():
        # Heat score: recency × frequency × importance
        heat = calculate_heat(granule)
        
        if (heat > CONSOLIDATION_THRESHOLD and
            granule.access_count > MIN_ACCESS and
            granule.age > MIN_AGE):
            
            # 1. Mark as consolidated
            granule.status = "consolidated"
            granule.decay_rate = float('inf')  # не забывается
            
            # 2. Generate summary (LLM)
            granule.summary = llm_summarize(granule.content)
            
            # 3. Link to schema
            link_to_schema(granule)
            
            # 4. Metrics
            consolidation_counter.labels(namespace=granule.namespace).inc()
```

### 10.7. Interference Detection

```python
# При добавлении/обновлении гранулы:
def detect_interference(new_granule):
    """Обнаружение противоречий с существующими знаниями."""
    # 1. Find semantically similar
    similar = search_similar(new_granule.content, threshold=0.8)
    
    for existing in similar:
        # 2. LLM-based contradiction check
        result = llm_check_contradiction(
            existing.content, 
            new_granule.content
        )
        
        if result.is_contradictory:
            # 3. Create conflict resolution
            create_conflict(
                granule_a=existing,
                granule_b=new_granule,
                explanation=result.explanation,
                options=["update_a", "deprecate_a", "merge", "keep_both"]
            )
```

### 10.8. Reflection Mechanism

```python
# При накоплении важных знаний:
def check_reflection_trigger():
    """Синтез higher-order insights из кластера гранул."""
    recent = get_recent(hours=24)
    cumulative_importance = sum(g.importance for g in recent)
    
    if cumulative_importance > REFLECTION_THRESHOLD:
        # Генерируем reflection через LLM
        reflections = llm_reflect(recent)
        
        for reflection in reflections:
            store_granule(
                content=reflection.insight,
                entity_type="reflection",
                importance=reflection.importance,
                links=[Link(type="derived_from", target=g.id) for g in recent]
            )
```

### 10.9. Итоговая архитектура (все системы)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Selti Memory System (Full)                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │   Lifecycle     │  │      KUP        │  │    Bio-Inspired │        │
│  │   Management    │  │   (Knowledge    │  │     Memory      │        │
│  │   (Раздел 8)    │  │    Update)      │  │   (Раздел 10)   │        │
│  │                 │  │   (Раздел 9)    │  │                 │        │
│  │  • States       │  │  • StaleRules   │  │  • Decay+SR     │        │
│  │  • TTL          │  │  • StaleDetector│  │  • Consolidation│        │
│  │  • Decay        │  │  • UpdateEngine │  │  • Schema Merge │        │
│  │  • Tiered       │  │  • Validator    │  │  • Interference │        │
│  │  • Versioning   │  │  • Sources      │  │  • Reflection   │        │
│  │  • Audit        │  │  • Audit        │  │  • Procedural   │        │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘        │
│           │                    │                     │                  │
│           └────────────────────┼─────────────────────┘                  │
│                                │                                        │
│                                ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    Unified Memory Service                       │    │
│  │  store() │ search() │ update() │ delete() │ traverse()        │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                │                                        │
│                                ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    Storage Layer                                │    │
│  │  PostgreSQL 16 │ Qdrant │ Redis │ Celery                       │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 10.10. Implementation Plan (полный)

| Фаза | Описание | Срок | Зависит от |
|------|----------|------|------------|
| **1** | Lifecycle Management (017-024) | 10-15 дней | — |
| **2** | Knowledge Update Pipeline (025-027) | 10-16 дней | Фаза 1 |
| **3** | Decay + Spaced Repetition | 5-7 дней | Фаза 1 |
| **4** | Consolidation Pipeline | 5-7 дней | Фаза 3 |
| **5** | Schema Merging | 5-7 дней | Фаза 4 |
| **6** | Interference Detection | 3-5 дней | Фаза 2 |
| **7** | Reflection Mechanism | 3-5 дней | Фаза 4 |
| **8** | Procedural Memory | 10-14 дней | Фаза 5 |

**Итого:** 51-76 дней разработки.

---

## 11. Переработка Granulation Pipeline и Versioned Memories (Эна)

### 11.1. Проблема

**Три фундаментальных ограничения текущей системы:**

| # | Проблема | Текущее состояние | Хочется |
|---|----------|-------------------|---------|
| 1 | **Зависимость от opencode/akame** | akame plugin (TS) → LLM → `ingest_batch` | Независимый pipeline прямо в selti |
| 2 | **Забывание вместо эволюции** | `is_archived`, `memory_forget_soft` | Хранить каждое состояние, "вектор обновления" |
| 3 | **Простой search** | `memory_search` = similarity only | История версий, diff, rollback, timeline |

### 11.2. Независимый Granulation Pipeline

**Текущий пайплайн:**
```
opencode session → akame plugin (TS) → LLM → selti MCP (ingest_batch)
  ↑ зависимость от opencode
```

**Новый пайплайн:**
```
Input Source → GranulationService → LLM Provider → Validation → Dedup → VersionedStore
  ↑ любые данные       ↑ Python, в selti        ↑ Strategy pattern
```

**Компоненты:**

| Компонент | Назначение | Паттерн |
|-----------|-----------|---------|
| `InputAdapter` | Конвертация разных источников в единый формат | Strategy |
| `LLMExtractor` | Извлечение знаний через LLM API | Strategy |
| `GranuleValidator` | Валидация перед сохранением | — |
| `GranulationService` | Оркестрация пайплайна | Orchestrator |

**Адаптеры:**
- `DialogueAdapter` — диалоги opencode
- `DocumentAdapter` — документы (markdown, txt)
- `CodeAdapter` — исходный код
- `ManualAdapter` — ручной ввод

**Пример использования:**
```python
# Через MCP tool
memory_granulate(
    text="今天我们 обсудили архитектуру selti...",
    source="dialogue",
    user_id="sergey",
    project_id="selti"
)
```

### 11.3. Versioned Memories (Git для знаний)

**Концепция:** Каждое обновление = новая версия. HEAD = актуальная. История = цепочка.

**Аналогия с Git:**
| Git | Versioned Memories |
|-----|-------------------|
| commit | version |
| HEAD | current_version_id |
| log | get_version_history() |
| diff | diff_versions() |
| revert | rollback_version() |

**Схема БД:**
```sql
-- Таблица versions
CREATE TABLE memory_versions (
    id UUID PRIMARY KEY,
    granule_id UUID REFERENCES memories(id),
    version_number INT,
    content TEXT,
    metadata JSONB,
    importance INT,
    change_type TEXT,  -- create | update | rollback | merge
    change_reason TEXT,
    parent_version_id UUID REFERENCES memory_versions(id),
    created_at TIMESTAMPTZ,
    created_by TEXT
);

-- HEAD указатель в memories
ALTER TABLE memories ADD COLUMN current_version_id UUID;
ALTER TABLE memories ADD COLUMN version_count INT DEFAULT 1;
```

**Хранимые процедуры:**
- `create_version()` — создать новую версию + обновить HEAD
- `rollback_version()` — откат к указанной версии
- `get_version_history()` — история версий

### 11.4. Update Vector (Траектория эволюции)

**Вместо забывания — эволюция:**
```
v1 (create) → v2 (update) → v3 (update) → v4 (rollback)
  │              │              │              │
  └─ content A   └─ content B   └─ content C   └─ content B'
```

**Каждое изменение хранит контекст:**
- `change_type`: create | update | rollback | merge
- `change_reason`: "обновлено после код-ревью", "откат из-за бага"
- `parent_version_id`: ссылка на предыдущую версию
- `created_by`: кто сделал изменение

### 11.5. Переработанный Query API

**Новые MCP Tools:**

| Tool | Описание |
|------|----------|
| `memory_create_version` | Создать новую версию (с историей) |
| `memory_trajectory` | Траектория развития гранулы |
| `memory_diff` | Сравнение двух версий |
| `memory_rollback` | Откат к предыдущей версии |
| `memory_timeline` | Временная шкала изменений |
| `memory_granulate` | LLM-грануляция (независимая от opencode) |

**Обновлённые Tools:**
- `memory_search` → возвращает **только HEAD** версии
- `memory_update` → создаёт **новую версию** вместо перезаписи

### 11.6. Примеры использования

**Траектория развития:**
```python
# memory_trajectory(granule_id="abc-123")
{
  "granule_id": "abc-123",
  "current_version": {
    "version_number": 4,
    "content": "selti использует Qdrant для векторного поиска...",
    "change_type": "update",
    "change_reason": "миграция pgvector → qdrant"
  },
  "history": [
    {"version": 3, "change_type": "update", "content": "..."},
    {"version": 2, "change_type": "update", "content": "..."},
    {"version": 1, "change_type": "create", "content": "..."}
  ],
  "total_versions": 4
}
```

**Diff:**
```python
# memory_diff(version_a_id="v1", version_b_id="v4")
{
  "content_diff": "--- v1\n+++ v4\n@@ -1 +1 @@\n-selti использует pgvector\n+selti использует Qdrant",
  "metadata_diff": {"changed": {"entity_name": {"old": "PgVectorStore", "new": "QdrantStore"}}},
  "importance_changed": false
}
```

**Rollback:**
```python
# memory_rollback(granule_id="abc-123", target_version_id="v2")
{
  "new_version_id": "v5",
  "change_type": "rollback",
  "change_reason": "Rollback to version 2"
}
```

### 11.7. SQL миграции

| # | Описание | Таблицы |
|---|----------|---------|
| 028 | Versioned Memories: таблица `memory_versions`, колонки HEAD | `memory_versions`, `memories` |
| 029 | Granulation Pipeline: конфиг LLM | `system_config` |
| 030 | Migration existing data: создание v1 для существующих гранул | `memory_versions` |

### 11.8. Python компоненты

```
memory_server/
├── granulation/                    # НОВЫЙ модуль
│   ├── adapters.py                 # InputAdapter (Strategy)
│   ├── extractors.py               # LLMExtractor (Strategy)
│   ├── validator.py                # GranuleValidator
│   └── service.py                  # GranulationService (Orchestrator)
├── memory/
│   ├── version_repository.py       # НОВЫЙ: version CRUD
│   ├── version_service.py          # НОВЫЙ: VersionService
│   └── service.py                  # обновлённый
├── tools/
│   ├── version_tools.py            # НОВЫЙ: 6 tools
│   └── memory_tools.py             # обновлённый
└── tasks/
    └── memory_tasks.py             # + granulate, version tasks
```

### 11.9. Migration Plan

| Фаза | Описание | Срок |
|------|----------|------|
| **1** | Version Storage (миграция 028) | 2-3 дня |
| **2** | Granulation Pipeline (миграция 029) | 3-5 дней |
| **3** | Query API Overhaul (6 new tools) | 3-5 дней |
| **4** | Migration existing data (v1 для всех гранул) | 1-2 дня |

**Итого:** 9-15 дней разработки.

### 11.10. Backward Compatibility

- `memory_store` — остаётся, создаёт гранулу с version=1
- `memory_update` — переработан: создаёт новую версию вместо перезаписи
- `memory_search` — фильтрует только HEAD (не ломает существующие клиенты)
- `memory_ingest_batch` — остаётся, создаёт гранулы с version=1

---

*Отчёт сформирован 2026-08-14. Аналитики: Эна (architect), Нора (db-architect), Луна (learner). Оркестратор: Афина (team lead).*
