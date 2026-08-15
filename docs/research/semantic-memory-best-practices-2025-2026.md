# Исследование семантической памяти: Best Practices 2025-2026

> Исследование проведено: Эна (Architect) — Argenta Team
> Дата: 2026-08-01
> Проект: selti (selti)

---

## 1. Аналогичные проекты

| Проект | Что делает | Чем полезен для selti |
|--------|-----------|------------------------------|
| **Mem0** (48K ⭐, $24M funding) | Universal memory layer. Two-phase pipeline: LLM extraction → conflict detection + graph update. v3 ADD-only архитектура. Multi-signal retrieval: semantic + BM25 + entity matching. 26% лучше OpenAI memory на LOCOMO. | **Conflict detection** при записи — сравнивает новые факты с существующими, мержит/обновляет/флагает. **Entity linking** — параллельная коллекция сущностей для boost релевантности. **Metadata filtering** — scope user > session > agent > org. |
| **Zep/Graphiti** (Neo4j) | Temporally-aware knowledge graph. **Bitemporal edge annotation**: event time + ingestion time. Hybrid search: semantic + BM25 + graph traversal. P95 latency 300ms. 94.8% на DMR. | **Bitemporal модель** — каждый факт имеет "когда был правдив" и "когда обнаружен". **Граф-обход** при поиске — не просто vector similarity, а traversal от найденных нод. **Hierarchical subgraphs**: episodic → semantic → community. |
| **Letta (MemGPT)** | OS-inspired tiered memory: core (in-context) → recall (searchable history) → archival (vector). **Self-editing** — агент сам управляет своей памятью через function calls. Virtual context management. | **Self-editing memory** — агент может сам обновлять core memory. **Memory pressure signals** — автоматическая компрессия при переполнении контекста. **Paging** между tier'ами. |
| **Cognee** (29K ⭐) | Open-source memory platform. ECL pipeline: Extract → Cognify (build graph) → Load. **Memify/improve** — пост-процессинг графа без пересборки. Self-improving memory с temporal awareness. | **Memify pipeline** — очистка stale нод, укрепление связей, reweighting важных memories БЕЗ полной пересборки графа. **Incremental updates** — только новые/обновлённые файлы обрабатываются. **Single Postgres** — graph + vectors + metadata в одной БД. |
| **GraphRAG (Microsoft)** | LLM-derived knowledge graphs. Index: NER + relation extraction → graph. Community hierarchy + summaries. Query: seed nodes → traversal → reranking. | **Community detection** — автокластеризация сущностей. **Summary per community** — LLM-саммари для каждого кластера. **Local + Global query** — разные стратегии для разных типов вопросов. |
| **CrewAI Memory** | 4 типа: short-term (ChromaDB), long-term (SQLite), entity (ChromaDB), user (Mem0). Разные backends для разных типов. | **Разделение по типам** — pragmatic подход, каждый тип оптимизирован под свою задачу. |

---

## 2. Best Practices 2025-2026

### 2.1. Three-Tier Memory Taxonomy (когнитивная модель)

Индустрия сошлась на трёхуровневой модели памяти:

| Уровень | Что хранит | Аналогия | Пример |
|---------|-----------|----------|--------|
| **Episodic** | Сырые события, сообщения, логи | Дневник / Ship's log | "Сегодня обсудили API авторизации" |
| **Semantic** | Факты, сущности, связи | Энциклопедия / Knowledge base | "Auth API использует JWT, порт 8080" |
| **Procedural** | Правила, конвенции, инструкции | Инструкция / CLAUDE.md | "Пишем тесты на pytest,覆盖率 > 80%" |

**Ключевой инсайт:** Использование одной стратегии поиска для всех трёх уровней — типичная ошибка. Temporal proximity важна для episodic, semantic relevance — для knowledge, freshness — для working memory.

### 2.2. Hybrid Search (Vector + Keyword + Graph)

**Статистика:** 95% RAG-систем в 2025 полагались ТОЛЬКО на vector embeddings. Гибридные архитектуры значительно превосходят.

**Production pattern (Zep/Graphiti):**
1. Semantic entry: vector similarity → candidate nodes
2. Graph traversal: от candidate'ов → relational context
3. Reranking: vector score + graph distance + keyword match
4. Context assembly для LLM

**Mem0 v3 retrieval pipeline:**
```
Query → Parallel scoring:
  ├── Semantic search (vector similarity)
  ├── BM25 keyword search (term matching)
  └── Entity matching (entity graph boost)
→ Score fusion → Results
```

**Результаты:**
- Zep: 94.8% на DMR (vs MemGPT 93.4%), P95 300ms
- Mem0: +29.6 points на temporal reasoning, +23.1 на multi-hop
- HybridRAG (VLIZ 2025): превосходит pure VectorRAG и pure GraphRAG

### 2.3. Temporal / Bitemporal Memory

**Проблема:** Факты меняются во времени. "Пользователь предпочитал TypeScript" может устареть.

**Решение (Zep/Graphiti) — Bitemporal model:**
- **Event time**: когда факт был правдив ("Пользователь выбрал TypeScript 15 марта")
- **Ingestion time**: когда агент это обнаружил ("Агент узнал 20 марта")

**Зачем:**
- Обработка противоречий без потери информации
- Time-travel queries: "Что пользователь предпочитал до ноября?"
- Версионирование фактов

### 2.4. Memory Consolidation (AutoDream / Memify)

**Проблема:** Память раздувается. 57% сирот в selti — типичная ситуация.

**Решение 1: AutoDream (Claude Code, 2026)**
- Background sub-agent работает во время простоя (idle periods)
- 4 фазы: Orientation → Gather Signal → Consolidate → Prune
- Операции: Pruning (удаление stale), Merging (дедупликация), Refreshing (обновление контекста)
- Аналогия с REM sleep: мозг консолидирует память во сне

**Решение 2: Cognee Memify**
- Пост-процессинг графа БЕЗ пересборки
- Очистка stale нод, укрепление частых связей, reweighting по recency/frequency
- Incremental updates — только новые/изменённые данные

**Результат:** Memory accuracy улучшается на 15-25% после consolidation.

### 2.5. Self-Editing Memory (Letta/MemGPT)

**Парадигма:** Агент сам управляет своей памятью через function calls:
- `core_memory_replace` — обновить факт
- `core_memory_append` — добавить факт
- `archival_memory_insert` — отправить в долгосрочное хранилище
- `conversation_search` — поиск по истории

**Memory pressure:** Когда контекст переполняется — агент сам решает что архивировать.

### 2.6. Scope Hierarchy (Multi-tenancy)

**Mem0 v3 scope model:**
```
user_id (primary scope)
  └── session_id / run_id (session scope)
        └── agent_id (agent scope)
              └── app_id / org_id (org scope)
```

**Композитные идентификаторы:** Запрос может сузить scope до конкретного user в конкретном session, или расширить до всех memories пользователя.

### 2.7. Memory Security (OWASP ASI06)

**Угроза 2025-2026:** Memory Poisoning — атакующий внедряет вредоносные инструкции в память агента. OWASP включил это в Top 10 для Agentic Applications.

**Защиты:**
- **Provenance tracking**: откуда пришла каждая запись
- **Trust scoring**: комбинирование temporal signals + content analysis
- **Anomaly detection**: мониторинг паттернов записи
- **Rollback**: снэпшоты для forensic analysis
- **Isolation**: сессионная память изолирована от cross-session

### 2.8. Schema-Grounded Memory

**Проблема:** LLM extraction может генерировать inconsistent entities/relations.

**Решение (Cognee, MemGraphRAG):**
- Schema-aware extraction: LLM извлекает сущности строго по ontology
- Iterative refinement: валидация + исправление после extraction
- Controlled vocabulary: типы сущностей и связей определены заранее

---

## 3. Предлагаемые фичи для selti

### Фича 1: Hybrid Search (Vector + BM25 + Graph Traversal)

- **Описание:** Многоканальный поисковый pipeline, который параллельно ищет по векторному сходству (Qdrant/pgvector), ключевым словам (BM25/FTS) и граф-обходу (traversal от найденных нод). Результаты fusion'ятся в единый score.

- **Зачем:** Текущий поиск в selti — ТОЛЬКО vector similarity (Qdrant) или FTS fallback. Это пропускает:
  - Точные совпадения терминов (BM25 ловит то, что vector пропускает)
  - Связанный контекст (graph traversal находит "соседей" найденных нод)
  - По benchmarks: +29.6 points на temporal reasoning, +23.1 на multi-hop

- **Как реализовать:**
  1. Добавить BM25 индекс в PostgreSQL (уже есть `search_fts` с tsvector — расширить)
  2. Создать `HybridSearchEngine`:
     ```
     query → parallel:
       ├── QdrantVectorStore.search() → vector results
       ├── PGRepository.search_fts() → keyword results
       └── GraphTraverse(seed_nodes) → graph results
     → Reciprocal Rank Fusion (RRF) → top-K
     ```
  3. Добавить graph traversal: от vector/keyword candidates → `get_relations()` → expand context
  4. Fusion strategy: RRF (Reciprocal Rank Fusion) или weighted sum

- **Приоритет:** **P0** — критичен для качества поиска
- **Effort:** **2-3 недели** (BM25 индекс + HybridSearchEngine + graph traversal + fusion + тесты)

---

### Фича 2: Bitemporal Memory (Event Time + Ingestion Time)

- **Описание:** Каждая гранула хранит два временных метки: `event_time` (когда факт был правдив/актуален) и `ingested_at` (когда запись появилась в базе). Поддержка time-travel queries и automatic staleness detection.

- **Зачем:** Сейчас все гранулы "вечные" — нет понятия "это было правдиво до ноября". При 3140+ гранулах невозможно понять что устарело. Противоречия ("пользователь любит TypeScript" vs "пользователь перешёл на Rust") не разрешаются.

- **Как реализовать:**
  1. Schema migration: добавить `event_time TIMESTAMPTZ` и `ingested_at TIMESTAMPTZ` в таблицу memories
  2. `ingested_at` = auto (DEFAULT NOW())
  3. `event_time` = опциональный параметр при записи (для фактов с известной датой)
  4. Поисковые фильтры: `WHERE event_time <= now()` для "текущих" фактов
  5. Versioning: при обновлении факта — старая версия архивируется с `expired_at`
  6. Time-travel query: `memory_search(query, as_of='2026-03-15')` — facts valid at that date

- **Приоритет:** **P1** — важно для долгосрочной памяти
- **Effort:** **2 недели** (migration + repository changes + search filters + versioning)

---

### Фича 3: Memory Consolidation (AutoDream)

- **Описание:** Фоновый процесс (background job), который периодически консолидирует память: очищает stale/duplicate гранулы, мержит похожие, обновляет weights по recency/frequency, удаляет сироты без связей.

- **Зачем:** 57% сирот (orphan granules) в текущем графе. 41 дубликат entity_name. Память раздувается и деградирует. Типичная проблема всех memory-систем решается consolidation.

- **Как реализовать:**
  1. Новый Celery periodic task `memory_consolidate` (запуск раз в 24ч или по trigger)
  2. Phase 1 — **Orphan detection**: найти гранулы без связей (degree=0), пометить как кандидаты на удаление
  3. Phase 2 — **Duplicate detection**: найти гранулы с похожим content (cosine > 0.95), мержить (оставлять более свежую, помечать старую как deprecated)
  4. Phase 3 — **Stale detection**: гранулы с `importance=1` и `updated_at > 90 дней` → пометить как archivable
  5. Phase 4 — **Weight update**: пересчитать importance на основе graph centrality (pageRank-like)
  6. Dry-run mode: показать что будет изменено БЕЗ реальных изменений
  7. Уведомление в `dialogue_insights` о результатах consolidation

- **Приоритет:** **P1** — критичен для поддержания качества памяти
- **Effort:** **2-3 недели** (Celery task + 4 фазы + dry-run + метрики + тесты)

---

### Фича 4: Entity Linking (Mem0-style)

- **Описание:** Параллельная коллекция сущностей (entities), которая извлекается из гранул при записи. При поиске — сначала матчатся сущности из запроса с коллекцией, потом их score boost'ит релевантные гранулы.

- **Зачем:** Сейчас поиск чисто семантический. Если ищем "auth module" — найдёт семантически похожее, но не обязательно связанное. Entity linking позволяет находить через конкретные сущности (классы, функции, файлы).

- **Как реализовать:**
  1. Новая таблица `entities` (id, name, type, embedding, metadata)
  2. При `memory_store`: LLM extraction сущностей из content → запись в `entities`
  3. При `memory_search`:
     - Query → LLM extraction entities → entity search (vector match)
     - Matched entities → find linked granules → boost their scores
  4. Fusion: final_score = α * vector_score + β * entity_boost + γ * graph_distance

- **Приоритет:** **P2** —nice-to-have для precision
- **Effort:** **2 недели** (schema + extraction pipeline + search integration)

---

### Фича 5: Memory Security & Provenance

- **Описание:** Отслеживание provenance каждой гранулы (откуда пришла, кто записал, когда). Trust scoring на основе temporal signals + source reliability. Anomaly detection на паттерны записи.

- **Зачем:** OWASP ASI06 включил Memory Poisoning в Top 10. При 11 агентах, пишущих в selti — каждый может случайно (или намеренно) внедрить некорректные данные. Нужна прослеживаемость и защита.

- **Как реализовать:**
  1. Schema: добавить `source_agent`, `source_session`, `trust_score`, `provenance_metadata` в memories
  2. При записи: автоматически заполнять provenance из MCP context
  3. Trust scoring: `trust_score = f(source_reliability, age, cross_references)`
  4. Anomaly detection: алерт если один agent записывает > N гранул в час
  5. Rollback: `memory_rollback(session_id)` — откатить все записи за сессию

- **Приоритет:** **P1** — безопасность критична
- **Effort:** **1.5 недели** (schema + provenance tracking + trust scoring + anomaly alerts)

---

## 4. Рекомендации по приоритетам

```
P0 (Сейчас):
  └── Hybrid Search (P0) — 2-3 недели
        Мгновенный эффект: +29.6 temporal, +23.1 multi-hop accuracy

P1 (Ближайший месяц):
  ├── Bitemporal Memory (P1) — 2 недели
  ├── Memory Consolidation (P1) — 2-3 недели
  └── Memory Security (P1) — 1.5 недели

P2 (Следующий квартал):
  └── Entity Linking (P2) — 2 недели
```

### Общий effort: 9.5-13.5 недель (2.5-3.5 месяца)

### Порядок реализации:

1. **Недели 1-3:** Hybrid Search (P0) — максимальный ROI
2. **Недели 4-5:** Bitemporal Memory (P1) — schema migration
3. **Недели 6-8:** Memory Consolidation (P1) — очистка текущего состояния
4. **Недели 9-10:** Memory Security (P1) — provenance + trust
5. **Недели 11-12:** Entity Linking (P2) — precision boost

### Матрица Impact vs Effort:

```
                    LOW EFFORT          HIGH EFFORT
                ┌─────────────────┬─────────────────┐
   HIGH IMPACT  │ Hybrid Search   │ Memory          │
                │ (P0)            │ Consolidation   │
                │                 │ (P1)            │
                ├─────────────────┼─────────────────┤
   LOW IMPACT   │ Memory Security │ Entity Linking  │
                │ (P1)            │ (P2)            │
                │                 │                 │
                └─────────────────┴─────────────────┘
```

---

## 5. Ссылки

- Mem0 paper: https://arxiv.org/abs/2504.19413
- Zep/Graphiti paper: https://arxiv.org/abs/2501.13956
- MemGPT paper: https://arxiv.org/abs/2310.08560
- Cognee: https://github.com/topoteretes/cognee
- GraphRAG: https://microsoft.github.io/graphrag
- OWASP ASI06: https://genai.owasp.org
- AutoDream: https://zenvanriel.com/ai-engineer-blog/claude-code-autodream-memory-consolidation-guide
- MAGMA (Multi-Graph Memory): https://arxiv.org/abs/2601.03236
- Synapse (Spreading Activation): https://aclanthology.org/2026.findings-acl.1108
- MemGraphRAG: https://arxiv.org/abs/2606.00610
