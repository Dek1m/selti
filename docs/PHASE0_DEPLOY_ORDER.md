# PHASE0_DEPLOY_ORDER — порядок применения Фазы 0 (для Рэя)

**Статус:** рабочий порядок деплоя миграций Фазы 0
**Дата:** 2026-09-17
**Автор:** Нора (db-architect)
**План:** `docs/PLAN_MEMORY_REDESIGN.md` (§0)
**Деплой запрещён** до команды Мастера — этот документ описывает строгий порядок на момент разрешения.

---

## 0. Критично перед стартом

1. **`migrations/run.py` идёт по алфавиту.** Он отсортирует `018b_drop_is_archived.sql` и `018c_drop_namespace_text.sql` между `018` и `019`, а `020` — после `018c`. Это **неверный** порядок Фазы 0. Поэтому батчи применяются **вручную** (см. ниже), а `018b`/`018c` должны быть **исключены из автоматического прогона** до их очереди.
2. **Полный снапшот БД** перед батчами I и III (см. §5 «Откаты»).
3. Деплой выполняется **на копии прода** с реальным объёмом данных, затем — на прод в окно.

---

## I. Стартовый батч миграций (схема + хранимки)

Порядок строго фиксированный, применяется вручную (НЕ через `run.py` «применить все»):

```
017_projects_registry.sql
018_memories_canonical.sql
019_project_contexts.sql
020_stored_procedures_canonical.sql
```

- **017** — реестр проектов (`projects`, `technologies`, `project_technologies`, `project_links`, сиды).
- **018** — канонические колонки `memories` (`status/valid_from/valid_to/ingested_at/confidence/supersedes/superseded_by/frozen/last_accessed_at/access_count/project_id`), FK, индексы, триггер `version`, расширение `relations.link_type`, **backfill** (`slug → project_id`, temporal-поля), **перенос `is_archived=true → status='retracted'`**.
- **019** — `project_contexts` + хранимка `project_context_snapshot` (предварительная версия с `is_archived`).
- **020** — пересоздание ВСЕХ хранимок под каноническую схему (см. `020_stored_procedures_canonical.sql`), переход дедуп-индекса на `(namespace_id, content_hash)` с `status`-предикатом, чистка мёртвых pgvector-хранимок.

> ⚠ **`018b` и `018c` здесь НЕ применяются.** Они ждут батча III (после деплоя кода). Исключите их из автопрогона `run.py` и из любых скриптов «применить все .sql по алфавиту».

**Проверка батча I:**
- Колонки `status`, `valid_to`, `project_id`, `namespace_id` — на месте; `is_archived` и `namespace TEXT` ещё живы (намеренно).
- Хранимки из `020` **не ссылаются** на `is_archived` и `namespace TEXT` (они уже на `status` + `namespace_id`), поэтому батч III (дроп) пройдёт без ошибок.

---

## II. Деплой кода (Сона, волна 2)

- Код переключается на новые колонки и хранимки: `queries.py` уже канонический (`namespace_id` + JOIN, `status='asserted' AND valid_to IS NULL`, `project_id`).
- Используемые хранимки после 020: `get_relations_unified`, `graph_stats_unified`, `graph_traverse_full`, `project_context_snapshot` (и `merge_similar_granules` скриптом).
- Внешний MCP-контракт не ломается (namespace как `uid` строка через JOIN; SQL-хранимки принимают/возвращают читаемый `namespace`).

> В этом окне код и хранимки работают на **новой** семантике, но физически `is_archived`/`namespace TEXT` ещё в БД (их дроп — батч III). Ни код, ни хранимки их больше не используют.

---

## III. Дроп legacy-колонок

Порядок:

```
018b_drop_is_archived.sql
018c_drop_namespace_text.sql
```

**Перед запуском** — подтвердить, что пересозданные в `020` хранимки **не ссылаются на дропаемое** (grep по sql-файлам: `is_archived`, `namespace` как колонка `m.namespace`):
- `memory_upsert`, `memory_insert_batch` — ON CONFLICT по `namespace_id`, `status`-предикат.
- `memory_forget_soft`, `graph_stats_unified`, `graph_traverse_full`, `list_with_count`, `merge_similar_granules`, `project_context_snapshot` — `status='asserted' AND valid_to IS NULL`, `namespace_id` + JOIN.

- **018b** — дроп `is_archived` + пересоздание partial-индексов (`active`, `graph_stats`, `entity_name`, `project_status`) на `status`-семантику.
- **018c** — дроп `namespace TEXT` + пересоздание namespace-индексов на `namespace_id` + VIEW `v_memories_readable`.

> Дедуп-индекс `idx_memories_content_hash_active` создаёт ТОЛЬКО `020` (на `namespace_id` + `status`). `018b`/`018c` его не трогают — это устранение конфликта имён/семантики, выявленное при проверке совместимости.

---

## IV. Post-checks

1. **Reconciliation PG ↔ Qdrant** — `reconcile_vectors` (Фаза 0.6): счёт + сверка по `content_hash`, отчёт о расхождениях.
2. **Smoke всех 23 тулов** — `memory_store`, `memory_search`, `memory_list`, `memory_recent`, `memory_stats`, `memory_get_relations`, `memory_traverse`, and etc. Проверить: актуальность (нет «воскресших» удалённых), поиск, граф.
3. **Backfill идемпотентность** — повторный запуск `018` даёт 0 изменений.
4. **Проверка Qdrant payload** — точки с актуальным `status`/`project_id` после перезаливки.

---

## 5. Откаты (down-скрипты + snapshot)

| Шаг | Как откатить |
|---|---|
| Батч I (017) | down-скрипт 017 (DROP проектных таблиц) |
| Батч I (018) | down-скрипт 018 (DROP колонок/индексов/триггера). Backfill additive — данные уходят вместе с колонками |
| Батч I (019) | down-скрипт 019 (DROP функции + `project_contexts`) |
| Батч I (020) | down-скрипт 020 (DROP новых функций; восстановление дедуп-индекса на `(namespace, content_hash)`) |
| Батч III (018b) | down-скрипт 018b (восстановить `is_archived` + старые индексы) |
| Батч III (018c) | down-скрипт 018c (восстановить `namespace TEXT` + старые индексы + DROP VIEW) |
| Перезаливка Qdrant | повторный `backfill_qdrant.py` по ПГ (идемпотентен — upsert по id) |

**Snapshot БД:**
- `pg_dump` полный дамп **перед батчем I** и **перед батчем III** (перед дропом колонок — самое опасное место).

---

## 6. Окно простоя поиска (перезаливка Qdrant)

- **Что:** перевод Qdrant payload на диету (без `content`) + добавление `project_id`/`status` + payload-индексы (`user_id`, `namespace`, `project_id`, `status`).
- **Как:** полная перезаливка **разом** инструментом `migrations/backfill_qdrant.py` (переиспользование: чтение ПГ → embedding API → upsert в Qdrant). НЕ двухпейлоадный период.
- **Окно:** поиск недоступен ~минуты (время переливки по фактическому объёму БД). Запускать в плановое окно.
- **После заливки:** удаление `content` из payload + пересборка коллекции (payload-индексы).
- **Сходятся расхождения:** reconciliation (Фаза 0.6, beat каждые 6 ч).

---

## Порядок одним взглядом

```
I.   017 → 018 → 019 → 020     (вручную, БЕЗ 018b/018c)
II.  деплой кода (Сона волна 2)
III. 018b → 018c               (после проверки хранимок 020)
IV.  reconciliation + smoke 23 тулов
```

Перед I и III — снапшот БД. 018b/018c исключены из автопрогона на всех этапах до III.