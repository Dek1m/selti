# PLAN_MEMORY_REDESIGN — Редизайн памяти selti

**Статус:** Draft → на декларацию Мастера
**Дата:** 2026-09-17
**Авторы:** Афина (свод), Эна (аудит, HEAD `93dfef0`), Луна (research 2025–2026), Тишь (фиксация)
**Основа:** аудит v0.18.2 + `PLAN_HIERARCHICAL_SEMANTIC_MEMORY.md` (Level 0–5) + `LOGGING_AUDIT.md` + research Графити/Mem0/Letta/Cognee
**Гранулы памяти:** директива `ec1f711f`, roadmap `90637ff3`, инсайты `20f13830`

---

## 0. Принципы

1. **Без legacy.** Внутренних двойных путей, shim-периодов, dual-write режимов и «временно оставленного старого кода» — НЕТ. Переписали → старое удалили той же фазой (директива Мастера). Внешний MCP-контракт — это интерфейс системы, а не legacy: существующие поля ответов не переименовываем, новые добавляем; при смене семантики (например `is_archived` → `status`) все внутренние запросы переводятся сразу, колонка-дубль дропается в той же миграции.
2. **После каждой фазы система рабочая** — деплой идёт фазами, Катерина прикрывает тестами до перехода к следующей.
3. **Единый путь исполнения — Celery-воркер** (директива Мастера): все операции, включая read-path тулов и REST-бэкенд веб-морды, идут через `celery_call`-мост. Многопоточность и контроль в одной точке. Быстрый асинхронный мост (Фаза 3) убирает штраф busy-wait.
4. **Язык-агностичность** — схема БД и алгоритмы не зависят от Python (задел на гипотетический будущий переезд; Rust — отложено, в план не входит). Выбор каркаса — см. Приложение A (анализ mia).
5. **Код на английском, комментарии на русском**, логирование — argenta-logging (`E:/Projects/Python/docs/LOGGING_STANDARD.md`).

## 0.1 Принятые решения (утверждены Мастером)

| # | Решение |
|---|---|
| D1 | Реестр проектов: `projects` (UUID, slug, name, description, kind, status, local_path, repo_url, docs_url, homepage_url, default_branch) + `technologies` + `project_technologies` + `project_links` |
| D2 | `memories.project_id` — UUID NULL FK → `projects.id`. NULL = глобальный слой (внепроектное знание). Привязка — по содержанию знания, не по месту разговора (правило Тиши) |
| D3 | Версионирование: supersedes/superseded_by цепочки в `memories` + `status` (asserted/superseded/retracted/uncertain). Без таблицы `memory_versions`. Bitemporal: `valid_from/valid_to` + `ingested_at` + `confidence` |
| D4 | Затухание — фича ранжирования: `score = relevance × recency_decay × importance` (decay 0.995/день, конфиг). Физический GC — только superseded > 90 дней. Ручной freeze для вечных фактов |
| D5 | Поиск: hybrid RRF (Qdrant + PG FTS `russian`), MMR, фильтр актуальности до обрезки limit |
| D6 | Qdrant payload на диету (без content) + payload-индексы + reconciliation PG↔Qdrant |
| D7 | Иерархия: Фаза 4 = Level 3–4 (schemas/insights через LLM-консолидацию), Level 5 — backlog. belief-модель вырезана, хватит `confidence` |
| D8 | Веб-морда «Google для памяти»: React+Vite+TS+Zustand+TanStack Query+sigma.js; бэкенд — FastAPI read-only рядом с MCP |
| D9 | «Облачко знаний»: тул `memory_context` + материализованные снапшоты + SessionStart-хук ZCode + глобальный state |
| D10 | Семантический поиск проектов — коллекция `projects` в Qdrant (pgvector выпилен миграцией 011, векторных колонок в PG больше не создаём) |

---

## Фаза 0 — Схема БД + гигиена (4–6 дней)

**Цель:** каноническая модель данных. Без неё всё остальное — надстройка над JSONB-хаосом.
**Исполнители:** Нора (миграции), Сона (код-адаптация), Катерина (тесты), Рэй (деплой миграций).

### 0.1 Миграция 017 — реестр проектов

- [ ] `projects`: id UUID PK, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, description TEXT, kind TEXT CHECK IN ('code','infra','domain','workspace','org'), status TEXT CHECK IN ('active','archived','frozen') DEFAULT 'active', local_path TEXT, repo_url TEXT, docs_url TEXT, homepage_url TEXT, default_branch TEXT DEFAULT 'main', created_at/updated_at TIMESTAMPTZ. Индексы: `idx_projects_kind`, `idx_projects_status`, `idx_projects_local_path`
- [ ] `technologies`: id UUID PK, name TEXT UNIQUE NOT NULL, category TEXT (lang/framework/db/tool/service), docs_url TEXT
- [ ] `project_technologies`: project_id FK, technology_id FK, version TEXT, purpose TEXT, PK (project_id, technology_id)
- [ ] `project_links`: id UUID PK, project_id FK, link_type TEXT CHECK IN ('repo','ci','docs','board','monitoring','adr','other'), url TEXT NOT NULL, title TEXT
- [ ] Сиды: `selti`, `akame`, `albedo`, `mia`, `belle`, `zcode-local` (kind='workspace'), `argenta-team` (kind='org')
- [ ] Семантический поиск проектов — коллекция `projects` в Qdrant (D10): payload {slug, name, kind, status}; эмбеддинг name+description; сервисный метод + синк при CRUD

### 0.2 Миграция 018 — memories: каноническая гранула

- [ ] Колонки: `project_id UUID NULL FK → projects.id`, `status TEXT CHECK IN ('asserted','superseded','retracted','uncertain') DEFAULT 'asserted'`, `valid_from TIMESTAMPTZ DEFAULT now()`, `valid_to TIMESTAMPTZ` (NULL = актуально), `ingested_at TIMESTAMPTZ DEFAULT now()`, `confidence REAL DEFAULT 1.0 CHECK 0..1`, `supersedes UUID NULL FK`, `superseded_by UUID NULL FK`, `frozen BOOLEAN DEFAULT false`, `last_accessed_at TIMESTAMPTZ`, `access_count INT DEFAULT 0`
- [ ] `version INTEGER` — фикс: инкремент в UPDATE_MEMORY (`queries.py:54-62`) + триггер на `updated_at`-события контента
- [ ] Мёртвые колонки `source_type`/`source_location` — наполнить при store (`models.py`, `queries.py:1-5`) или удалить; решение — заполнять (дешевле, чем дроп)
- [ ] **`is_archived` → `status` сразу, без колонки-дубля:** перевести ВСЕ внутренние запросы (search/list/recent/stats/traverse/dedup/forget_soft — `is_archived=false` → `status='asserted' AND valid_to IS NULL`; forget_soft → `status='retracted'`), дропнуть колонку в этой же миграции. Никаких двухсемантичных периодов
- [ ] Дубликат `namespace TEXT` — дропнуть сразу (остаётся `namespace_id UUID` + generated column для читаемости)
- [ ] Индексы: GIN `metadata jsonb_path_ops`; expression `(metadata->>'entity_name')` WHERE is_archived=false (убивает seq scan `queries.py:32-37`); `(project_id, status)` partial; `(supersedes)`; `(valid_to)` partial WHERE NULL
- [ ] `relations`: расширить CHECK link_type — `+ supersedes, supports, member_of, part_of, describes_cluster` (`005_relations.sql:19-33`); дедуп точных дублей soft-resolve `(source_id, target_name, link_type)` WHERE target_id IS NULL (оставить min(id), CTE row_number, идемпотентно) + unique index на `(source_id, target_name, link_type)` WHERE target_id IS NULL (fan-in сохранён; дедуп — ПЕРЕД индексом)
- [ ] `namespaces`: таблица = единственный source of truth; enum `Namespace` в `config.py:7-13` удалить; auto-register с валидацией `^[a-z_]{1,64}$` + лимит 64/юзер (`namespace_repository.py:65-76`)
- [ ] Backfill (transactional, батчами по 500): slug из `metadata->>'project_id'` → FK ('unknown' → NULL); `status='asserted'` всем; `valid_from=created_at`, `ingested_at=created_at`; отчёт о непривязанных slug'ах
- [ ] Обновить Qdrant payload всех существующих точек: +project_id (UUID), +status — **полная перезаливка разом** инструментом `migrations/backfill_qdrant.py` (переиспользовать), НЕ двухпейлоадный период; после заливки — удаление content из payload и пересборка коллекции

### 0.3 Миграция 019 — project_contexts (под облачко знаний, D9)

- [ ] `project_contexts`: project_id UUID PK FK, content TEXT, sections JSONB, granule_count INT, computed_at TIMESTAMPTZ, metadata JSONB
- [ ] Хранимка `project_context_snapshot(p_project_id UUID)` по паттерну `list_with_count` (`014`): выборка топ-гранул `WHERE project_id=$1 AND status='asserted' AND is_archived=false` ORDER BY importance DESC, updated_at DESC с квотами по namespace (project_meta ×10, code_knowledge ×15, dialogue_insights ×5, infrastructure ×5)

### 0.4 Код-адаптация (Сона)

- [ ] **`SeltiState` — composition root (Приложение A, Вариант B):** единый объект-состояние (pool, Redis, Qdrant-клиент, circuit breaker, реестр сервисов memory/hash/context/projects); ленивые синглтоны; замена `_get_service()` (`memory_tasks.py:27-61`) и разрозненных фабрик; модульная группировка `tools/` (memory/hash/context/projects)

- [ ] `models.py`: MemoryRecord += project_id, status, confidence, valid_from/valid_to, supersedes/superseded_by, frozen, access-поля; Pydantic-контракты `metadata` с `schema_version`
- [ ] `db/queries.py` + `pg_repository.py`: INSERT/UPDATE/SELECT — новые колонки; `fetch_project_context()`, `upsert_project_context()`
- [ ] `interfaces.py` + `repository.py` (facade): +3-4 метода протокола
- [ ] `qdrant_store.py`: `build_filter` — project_id, status (`qdrant_store.py:176-186`); payload-индексы user_id/namespace/project_id/status в `setup_qdrant_collection.py`; payload → минимум БЕЗ content (D6)

### 0.5 Гигиена P2 (параллельно, Сона)

- [ ] Удалить `benchmark_celery.py`; `merge_similar_granules.py` → `scripts/`
- [ ] Shims: `repository_qdrant.py`, `logging_config.py` — перевести импорты (`dedup.py:8`, `service.py:11`, `__main__.py:260`) и удалить
- [ ] Label-баг `QDRANT_OPS`: `update_vector`/`set_payload` помечены `operation="upsert"` (`qdrant_store.py:114,128`)
- [ ] Debug-логи `ingest_batch` (`memory_tasks.py:632-643`) — удалить

### 0.6 Reconciliation PG↔Qdrant

- [ ] Celery-задача `reconcile_vectors` (beat, раз в 6ч): счёт + выборочная сверка по content_hash; отчёт о расхождениях; автофикс: рестайл точек без PG-двойника (delete), доupsert пропусков; метрика `reconciliation_diff_count`

**Критерии приёмки Фазы 0:** все миграции зелёные на копии прода; backfill идемпотентен (повторный запуск = 0 изменений); тесты Катерины: schema-контракты + backfill + build_filter; `memory_search`/`store` не сломаны (совместимость); reconciliation-отчёт сходится 100% на пустом диффе.

---

## Фаза 1 — Поиск и дедуп (4–6 дней)

**Цель:** выдача, которой можно доверять: релевантность × свежесть × важность. Исполнители: Сона, Катерина.

### 1.1 Hybrid search

- [ ] Фича-флаг `hybrid_search_enabled` (config), постепенный раскат
- [ ] Канал A: Qdrant dense (есть); канал B: PG FTS `tsvector('russian')` (`search_fts` уже есть — сменить конфиг `'simple'` → `'russian'`, `queries.py:44-49`)
- [ ] RRF-fusion: `score(d) = Σ 1/(60 + rank)`, prefetch по 100 на канал (`docs/vector-db-research-2026.md` §6)
- [ ] MMR-реранкер топ-K (λ=0.7) для разнообразия

### 1.2 Ранжирование (D4)

- [ ] Финальный score: `rrf_score × recency_decay × importance_weight`, decay = `0.995^(дней с last_accessed/created_at)`, конфиги per-namespace
- [ ] `access_count`/`last_accessed_at` — инкремент при выдаче (батч-UPDATE после search, вне транзакции чтения)
- [ ] frozen-гранулы — не затухают

### 1.3 Фильтр актуальности

- [ ] Qdrant payload-фильтр `status=asserted AND valid_to IS NULL` ДО обрезки limit (сегодня archived съедают лимит: `pg_repository.py:182` — фильтр после)
- [ ] Флаг `include_historical: bool = false` в memory_search → time-travel запросы

### 1.4 Дедуп

- [ ] Exact-фаза: batch-запрос `WHERE (namespace, content_hash) IN (...)` вместо цикла (`dedup.py:162-173`)
- [ ] Semantic-фаза: параллельные проверки (asyncio.gather + AsyncQdrantClient) вместо serial (`dedup.py:186-199`)
- [ ] Ограничение кандидатов парами сущностей (D7/research): compare только в пределах общих entity_name/namespace
- [ ] user_facts UPDATE-ветка: обновлять и content, не только metadata (`service.py:71-81`)

### 1.5 Traverse

- [ ] Cap узлов (default 500) + курсорная пагинация (`pg_repository.py:430-437`)

**Критерии приёмки:** golden-set запросов (50 шт: ru/en, точные/размытые) — recall@10 ≥ baseline+15%; latency search p95 ≤ 800мс при hybrid on; дедуп-батч 50 гранул ≤ 30с; тесты MMR/RRF на фикс. корпусе.

---

## Фаза 2 — Жизненный цикл гранул (5–8 дней)

**Цель:** у гранулы есть судьба: рождение → актуальность → supersession → GC. Исполнители: Сона (API), Нора (beat-задачи), Катерина.

### 2.1 Supersession API (D3)

- [ ] `MemoryService.create_version(project_id, new_content, ...)`: старая гранула → `status='superseded'`, `valid_to=valid_from новой` (правило Graphiti: окно закрывается моментом появления нового), `superseded_by=id новой`; новая → `supersedes=старая`; версия +1; Qdrant-пейлоады синхронно
- [ ] `memory_update` через service → вызывает supersession (не затирание!); merge metadata (`queries.py:57` COALESCE → dict-merge)
- [ ] `get_history(granule_id)`: проход по supersedes-цепочке (CTE recursive)
- [ ] `retract(granule_id, reason)` → status='retracted', valid_to=now()

### 2.2 Celery beat: жизнь памяти

- [ ] `confidence_decay` (ежедневно): confidence *= decay-фактор по отсутствию подтверждений (конфиг per-namespace); frozen не трогаем
- [ ] `mark_stale` (ежедневно): гранулы с confidence < порога и без access > N дней → флаг «кандидат на ревизию» (не удаляем — report в metrics + опциональный тул-запрос «чего устарело»)
- [ ] `gc_superseded` (еженедельно): hard delete superseded-версий старше 90 дней (конфиг), с предварительным отчётом; valid_to-история при этом сохраняется в цепочке
- [ ] `orphans_cleanup`: связи на несуществующие гранулы, пустые кластеры

### 2.3 Кластеризация (Level 2)

- [ ] Периодическая задача поверх хранимки `merge_similar_granules` (миграция 016, скрипт `scripts/`): threshold-кластеризация по namespace, ночью по расписанию
- [ ] Таблица `clusters` + колонка `cluster_id` в memories (принадлежность одним полем, relation `member_of` для кластеров не используем)

**Критерии приёмки:** сценарный тест: store A → store A′ (конфликт) → A.status=superseded, A.valid_to=A′.valid_from, get_history возвращает [A′,A]; GC после «90 дней» (time-mock) удаляет только superseded; decay не трогает frozen; regression: дедуп, search.

---

## Фаза 3 — API универсальность + наблюдаемость (4–6 дней)

**Цель:** project_id first-class во всех тулax; логи и метрики по стандарту. Исполнители: Сона, Мая (метрики), Тиамат (доки).

### 3.1 project_id везде (D2)

- [ ] `project_id: str | UUID | None` опциональным параметром в store/search/find_similar/list/recent/traverse/link/update (`memory_tools.py`); slug ↔ UUID резолв сервисом (кеш словаря)
- [ ] Qdrant-фильтр project_id при явном указании; без — поиск по всему (глобальный слой включён)
- [ ] hash-tools: `project` → UUID FK (единый формат с memories; `queries.py:297`)

### 3.2 Новые тулы

- [ ] `memory_get_history(granule_id)` — supersession-цепочка
- [ ] `memory_cluster_list(namespace, project_id?)` — Level 2 обзор
- [ ] `memory_context(project_id, refresh=False)` — Фаза 6 предпосылка (снапшот, см. 0.3/6.1)
- [ ] LinkType Literal-валидация на входе (`models.py:74`) — 400 вместо 500; документация типов в README

### 3.3 Наблюдаемость

- [ ] Дедуп логов: service-INFO → DEBUG (`service.py:190+`), остаётся 1 INFO/tool_handler; task_bridge SEND/OK → DEBUG (`task_bridge.py:35,60`)
- [ ] `get_logger` во всех 9 модулях (список в п.12 требований)
- [ ] Исключения не глотать: `metrics_decorator.py:39`, `service.py:238-239` — лог с request_id + re-raise/признак в ответе
- [ ] busy-wait task_bridge → asyncio-friendly ожидание (poll 100ms → wait/notify через Redis BLPOP или async result) (`task_bridge.py:47-51`)
- [ ] Метрики качества: `search_hit_rate` (клик→использование гранулы — прокси: факт последующей ссылки), `zero_result_searches`, `hybrid_vs_dense_latency`; дашборд-секция Prometheus
- [ ] Redis health — переиспользовать singleton client (`__main__.py:168-171`); liveness `/live` (лёгкий) vs readiness `/health`

**Критерии приёмки:** все 23+4 тула принимают project_id и проходят тесты матрицы (namespace × project × global-NULL); на операцию ≤ 1 INFO-запись; grep подтверждает отсутствие logging.getLogger мимо фасада; p95 тулов ≤ 300мс (без эмбеддинга).

---

## Фаза 4 — Иерархия знаний Level 3–4 (5–10 дней)

**Цель:** selti не только хранит, но и обобщает: schemas (устойчивые паттерны) и insights (мета-выводы). Исполнители: Эна (дизайн пайплайна), Сона, Тишь (LLM-грануляция), Катерина.

- [ ] Модель консолидации: LLM-провайдер (локальный vLLM 10.0.0.21 рядом с embedding — рекомендация; точное решение при старте фазы)
- [ ] Триггер «накопленная importance»: суммарный importance новых гранул с прошлой консолидации > порога (аналог 150 generative-agents; для шкалы 1–5 ≈ 20) → Celery-задача `consolidate(namespace, project_id)`
- [ ] Пайплайн: выбрать кластер/гранулы → LLM-саммари «схемы» → **новая гранула** namespace=`project_meta`/`insights` с relation `derived_from`/`abstracts_from` на исходники (D7: абстракция = новый узел, НЕ merge); confidence наследуется ×0.9
- [ ] Зеркало назад: абстракции ищутся вместе с исходниками (retrieval-boost: если schema в выдаче — подтягиваем исходники lineage)
- [ ] KUP-фаза (knowledge update pipeline): правила-детекторы устаревания (по образцу поглотившего pgvector→Qdrant кейса) — конфигурируемые проверки + mark_stale
- [ ] Level 5 (теории/модель мира) — backlog, не в этой фазе

**Критерии приёмки:** консолидация демо-датасета (акаме-гранулы) даёт связные схемы (ручная оценка Тиши/Мастера); исходники остаются нетронутыми; цикл консолидации идемпотентен (повтор не плодит дублей — dedup ловит).

---

## Фаза 5 — Веб-морда + REST (8–12 дней)

**Цель:** «Google для памяти» — человеческий интерфейс. Исполнители: Сона (REST+UI-каркас), Эна (UX-схема), Катерина (e2e), Тиамат.

### 5.1 FastAPI read-only (+минимум админ)

- [ ] Роутер в том же процессе (`__main__.py`): `/api/search` (hybrid, фильтры namespace/project_id/даты/статус), `/api/memories/{id}` (+history), `/api/graph/{id}` (traverse с caps), `/api/stats`, `/api/projects` CRUD-минимум (slug/name/desc/kind/local_path/links/technologies), `/api/contexts/{slug}` (облачко-превью)
- [ ] Auth: только localhost + опциональный token; CORS под фронт-порт
- [ ] **Единый путь исполнения (принцип 3):** REST-эндпоинты вызывают операции через `celery_call`-мост — как все тула; никакого прямого MemoryService в web-процессе. Быстрый мост из Фазы 3 (без busy-wait) делает цену приемлемой

### 5.2 Фронт (React+Vite+TS, D8)

- [ ] Каркас: Vite+React+TS, Zustand, TanStack Query, react-router; тёмная тема, токены (по образцу albedo tokens.css), bootstrap-icons
- [ ] Экран «Поиск»: строка-центр (google-стайл), фильтры-чипы (namespace, project, дата, статус), выдача = карточки гранул (score-объяснение: rrf × decay × importance)
- [ ] Карточка гранулы: content, metadata-таблица, lineage-версий (визуальная цепочка supersedes), связи (входящие/исходящие relations)
- [ ] Экран «Граф»: sigma.js/@react-sigma, узлы=гранулы (цвет=namespace, размер=importance), рёбра=relations; клик = карточка; фильтры; поиск-центровка
- [ ] Экран «Проекты»: реестр (таблица+карточка), стек-теги, линки, облачко-превью
- [ ] Экран «Статистика»: метрики из /api/stats + Prometheus-экспорт (рост, дедуп-рейшо, hit-rate)

**Критерии приёмки:** поиск из UI < 1с до первых результатов; граф 5К узлов интерактивен (webgl); e2e: поиск→карточка→lineage→граф; build+lint зелёные.

---

## Фаза 6 — Облачко знаний + ZCode-хуки (3–5 дней)

**Цель:** автоматическая инъекция контекста в сессии ZCode (D9, исходная задача). Исполнители: Сона (тул+хук), Тишь (сборка прозы), Рэй (конфиг машины).

### 6.1 Тул memory_context (достроить)

- [ ] `MemoryService.get_context(slug)` → Redis-кеш `ctx:{slug}` (TTL=beat-периоду) → таблица `project_contexts`; fast-path без Celery; `refresh=True` — немедленный пересчёт
- [ ] Celery beat `rebuild_contexts` (почасово) + dirty-флаг `ctx:{slug}:dirty` при store/update с project_id
- [ ] Секции снапшота: стек (из project_technologies!), ADR/решения (project_meta), топ-гранулы code_knowledge, последние dialogue_insights, инфраструктура

### 6.2 Сборка «прозы» Тишей

- [ ] Промпт-шаблон Тиши: SQL-снапшот → связный текст ≤ 100 строк (стек, архитектура, практики, что в работе); результат — sections.prose
- [ ] Запуск: по расписанию Тиши (после грануляционных циклов) или триггером от rebuild; всегда доступен механический fallback (снапшот без прозы)

### 6.3 ZCode-хук

- [ ] Скрипт `~/.zcode/hooks/knowledge-cloud.js` (node, type: "process", timeoutMs 5000): env `${ZCODE_PROJECT_DIR}` → матч local_path → slug → selti HTTP `memory_context` + `~/.zcode/state.md`; stdout `{"hookEventName":"SessionStart","additionalContext":"..."}`
- [ ] Graceful degradation: любой сбой → `{"hookEventName":"SessionStart"}` пусто + exit 0
- [ ] `~/.zcode/state.md` (2 строки: «в работе / завершено») — ведёт Тишь в фоне (уже читает rollout-файлы)
- [ ] Конфиг: `hooks.enabled:true` + SessionStart matcher `startup|resume` в `~/.zcode/cli/config.json`

**Критерии приёмки:** старт сессии в E:\Projects\Python\selti подкладывает облачко < 5с; в default-воркспейсе — только state; selti выключен → сессия стартует без задержек и без текста.

---

## Риски и откаты

| Риск | Митигация |
|---|---|
| Миграция 018 (backfill) на живых данных | Транзакционные батчи по 500; dry-run-режим; снапшот БД перед запуском; каждая миграция с down-скриптом (кроме backfill — он additive) |
| `is_archived` → `status` без переходного периода: широкий фронт правок запросов | Один PR на миграцию+код, grep-аудит всех вхождений до мерджа; Катерина: regression по всем 23 тулам на копии БД до деплоя |
| Полная перезаливка Qdrant payload | Идемпотентный backfill-скрипт (уже обкатан); downtime поиска ~минуты — запускать в окно; расхождение ловит reconciliation из Ф0 |
| Hybrid search деградирует выдачу | Фича-флаг, A/B на golden-set, откат = флаг off |
| GC удаляет нужное | GC работает ТОЛЬКО по superseded (у которых есть наследник) + dry-run отчёт первый месяц |
| Расползание сроков Фазы 4 (LLM) | Фаза изолирована: без неё 0–3+5–6 полностью ценны; start после стабилизации |
| Морда (Фаза 5) отвлекает от ядра | Порядок фаз 0→1→2→3 строгий; 5 и 6 можно менять местами по настроению Мастера |
| REST через Celery: +50–150мс на запрос UI | Приемлемо для внутренней тулзы; быстрый мост Фазы 3 (async-wait вместо busy-wait) снимает большую часть штрафа; при реальных болях — отдельная очередь `api` с приоритетом |

## Внешние контракты (не legacy)

- Сигнатуры существующих тулов не переименовываются; project_id/status/include_historical — опциональные параметры (это расширение интерфейса, не dual-path)
- JSON ответов только расширяется (новые поля в конце)
- `metadata` остаётся свободным JSONB (контракт `schema_version` — рекомендация, не требование)
- Внутренние shim'ы, старые ветки кода, колонки-дубли — удаляются в фазе внедрения замены, без «временно оставим»

## Порядок тестирования

1. Фаза 0: unit (schema-контракты) + интеграционные (миграции на копии) + regression MCP-тулов
2. Фаза 1–3: golden-set поиска, сценарные (supersession/GC/decay с time-mock), перфоманс-пороги
3. Фаза 4: ручная оценка качества консолидации (Тишь+Мастер)
4. Фаза 5: vitest unit + playwright e2e
5. После каждой фазы: деплой Рэя → smoke на проде → Тишь гранулирует решения фазы

## Итоговая таблица

| Фаза | Суть | Исполнители | Дни | Зависимости |
|---|---|---|---|---|
| 0 | Схема БД + гигиена | Нора, Сона, Катерина, Рэй | 4–6 | — |
| 1 | Поиск и дедуп | Сона, Катерина | 4–6 | Ф0 |
| 2 | Жизненный цикл | Сона, Нора, Катерина | 5–8 | Ф0 |
| 3 | API + наблюдаемость | Сона, Мая, Тиамат | 4–6 | Ф1, Ф2 |
| 4 | Иерархия L3–L4 | Эна, Сона, Тишь | 5–10 | Ф2, Ф3 |
| 5 | Веб-морда + REST | Сона, Эна, Катерина, Тиамат | 8–12 | Ф3 |
| 6 | Облачко + хуки | Сона, Тишь, Рэй | 3–5 | Ф0, Ф3 |
| **Σ** | | | **33–53** | |

Фазы 1 и 2 параллелятся (разные зоны); 5 и 6 — взаимно независимы, порядок по выбору Мастера.

---

## Приложение A — Анализ: State-класс и mia как корень selti

**Вопрос Мастера:** завести state-класс с состоянием или переиспользовать mia как корень для selti.

### Что mia даёт фактически (проверено по коду, v0.0.0)

- `Application` — Composition Root + `State`: загрузка/выгрузка модулей (`ModuleBase`, `on_load/on_unload`), API-прокси `state.api.<module>.<method>`
- `@api_method(parallel=True)` — выполнение в отдельном потоке; `@task` — Universal Task System: Redis-очередь + `mia-worker` (свой аналог Celery, `QueueDispatcher`; `MIA_DISPATCH=local` — in-process)
- `EventBus` (in-process pub/sub), `resilience` (circuit_breaker, retry, shutdown_manager), `storage` (cache_hierarchy, shared_memory, serializer), `ServiceRegistry`, фабрики Cache/Database/EventBus, роли процессов `belle`/`belle-worker` (api/worker/all)
- argenta-logging уже внутри

### Вариант A — переезд selti под mia целиком (selti = набор модулей mia)

Плюсы: единый каркас команды, модульность из коробки, один воркер-механизм вместо Celery, EventBus для реактивности (rebuild облачка по событию), belle-роли покрывают deploy.
Минусы (решающие): mia **v0.0.0, активная разработка** — каркас будет качаться под нами; замена Celery на самописный dispatch теряет зрелую экосистему (beat, retries, backoff, Flower, инспекция, multiprocess Prometheus — всё это у нас уже работает и связано с задачами Фаз 0–2); двойной рефакторинг (сначала схема/алгоритмы, потом каркас) растянет и так большой проект; связывание судьбы боевого selti с нестабильной зависимостью.

**Вердикт: не сейчас.** Ревизия — после стабилизации mia (v0.1+), отдельно от редизайна.

### Вариант B — принять паттерн, не каркас (рекомендуется в Фазу 0)

Из mia берём идеи, из экосистемы — Celery:

1. **`SeltiState` — composition root с состоянием** (по образцу mia `Application`): единый объект, владеющий pool/Redis/Qdrant-клиентами, service-реестром (memory/hash/context/projects), circuit breaker'ом; ленивые синглтоны вместо ныне разрозненных `_get_service()` (`memory_tasks.py:27-61`) и фабрик по месту; health-инспект по компонентам. Заменяет текущий набор параллельных синглтонов в web-процессе и воркере.
2. **Модульная нарезка тулов**: `tools/` группируется в модули (memory, hash, context, projects) с явным реестром — подготовка к потенциальному mia-переезду: каждый модуль = будущий `ModuleBase`.
3. **EventBus-стиль инвалидации**: dirty-флаги облачка и reconciliation — через события (Redis pub/sub), а не разрозненные ключи (Celery-совместимо, паттерн mia).
4. Всё по-прежнему через Celery (принцип 3) — @task mia не внедряем.

Цена: ~1–2 дня в Фазе 0 поверх миграций. Эффект: чище, тестируемее, и путь к «selti под mia» остаётся открытым без повторной работы.

### Дополнительные улучшения алгоритмов (директива «улучшайте, где можно»)

- Дедуп: LSH-претагирование кандидатов перед cosine (гранула `selti-lsh-dedup` из памяти уже обосновывает) — в Фазу 1 как фича-флаг
- Кеш-иерархия mia-стайл (L1 in-process → L2 Redis) для эмбеддингов и облачка — в Фазу 6 при построении `memory_context`
- Сводное ранжирование — уже в Фазе 1; при появлении абстракций Фазы 4 — graph-distance boost (research Луны)
