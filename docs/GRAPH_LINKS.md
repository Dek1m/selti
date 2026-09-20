# Как устроены связи в selti

Документация графа знаний для инженеров команды. Описывает полный жизненный цикл
связи: от рождения в `metadata.links` до чтения через `get_relations_unified` и
смерти в ретракте или GC.

> Схема данных — миграция [005_relations.sql](../migrations/005_relations.sql).
> Целевая архитектура v2 (резолв имён, confidence, OrphanLinker) —
> [GRAPH_LINKS_V2_ARCHITECTURE.md](GRAPH_LINKS_V2_ARCHITECTURE.md).

---

## 1. Жизненный цикл связи

```
 РОЖДЕНИЕ                СИНХРОНИЗАЦИЯ              ЧТЕНИЕ                  СМЕРТЬ
┌──────────────┐   ┌─────────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ Тишь:        │   │ sync_links_batch    │   │ get_relations_   │   │ ручные:          │
│  metadata.   │──▶│ (delete+insert,     │──▶│ unified (014/020)│──▶│  memory_unlink   │
│  links       │   │  synced_from)       │   │ = outgoing UNION │   │ синковые:        │
│ memory_link: │   │                     │   │  ALL incoming    │   │  перезапись синком│
│  INSERT в    │   │ memory_store/update │   │                  │   │ GC:              │
│  relations   │   │ → sync_links_to_    │   │ traverse/stats — │   │  CASCADE /       │
│ (будущее)    │   │  relations          │   │  graph_traverse_ │   │  SET NULL →      │
│  OrphanLinker│   │                     │   │  full, stats_    │   │  orphans_cleanup │
└──────────────┘   └─────────────────────┘   └──────────────────┘   └──────────────────┘
```

### 1.1 Рождение

| Источник | Куда попадает | Кто пишет |
|---|---|---|
| `metadata.links` гранулы | JSONB в `memories.metadata` | Тишь (`memory_store`), формат: `[{"type", "target", "description"}]` |
| `memory_link` | Прямой `INSERT INTO relations` | Клиент/агент через MCP |
| auto_linker | Планируется (фаза F плана v2) | — |

`metadata.links` — **черновик**, а не ребро графа. Ребро появляется только после
синхронизации (п. 1.2).

### 1.2 Синхронизация: metadata.links → relations

Синк — это **delete + insert** одним SQL-батчем:

- одиночный: `sync_links_to_relations` → [queries.py: `BACKFILL_RELATIONS_FROM_METADATA`](../memory_server/db/queries.py)
- батчевый (после batch-инга): [memory_tasks.py:729](../memory_server/tasks/memory_tasks.py) → `SYNC_LINKS_BATCH`

Алгоритм `SYNC_LINKS_BATCH`:

1. Удалить у гранул все связи с пометкой `synced_from = 'metadata.links'` (ручные не трогаются).
2. Разобрать `metadata.links`, для каждой ссылки:
   - `target` — UUID и гранула существует → вставить с `target_id`;
   - `target` — UUID, но гранулы нет → связь **не создаётся**;
   - `target` — не UUID → вставить с `target_name`, `target_id = NULL` (сирота, см. § 6);
   - `weight` — всегда `1.0` (захардкожен; формула confidence — § 5, план v2);
   - `metadata` — `{"synced_from": "metadata.links"}`.
3. `ON CONFLICT (source_id, target_id, link_type)` — апдейт description/weight, дублей нет.

Точки вызова: `store` ([service.py:204](../memory_server/memory/service.py)),
`update` ([service.py:408](../memory_server/memory/service.py)),
batch-инжест ([memory_tasks.py:729](../memory_server/tasks/memory_tasks.py)).
Синк **non-fatal**: его падение не ломает store, но ребро в графе не появится.

### 1.3 Чтение

Чтение всегда идёт из таблицы `relations` — хранимка
[`get_relations_unified`](../migrations/014_stored_procedures_optimizations.sql)
(тело под каноническую схему — миграция 020):

- `UNION ALL` исходящих (`source_id = :id`) и входящих (`target_id = :id`) одним round-trip;
- колонка `direction` (`outgoing` / `incoming`) — добавляется на стороне SQL;
- `UNION ALL`, а не `UNION`: наборы не пересекаются по определению, дедупликация не нужна.

Обёртки в Python: `get_relations` → `RelationListResult {incoming, outgoing}`
([service.py:1079](../memory_server/memory/service.py)). Обход графа — `graph_traverse_full`,
статистика — `graph_stats_unified` (та же миграция 014/020).

### 1.4 Смерть

| Событие | Что происходит со связями | Где |
|---|---|---|
| `memory_unlink` | Явное удаление одной связи | `DELETE_RELATION` |
| Ретракт/обновление `metadata.links` | Удаляются только синковые (`synced_from='metadata.links'`), пересоздаются следующим синком | `DELETE_SYNCED_RELATIONS` |
| Hard delete гранулы-**источника** | `ON DELETE CASCADE` — связи умирают вместе с гранулой | миграция 005 |
| Hard delete гранулы-**цели** | `ON DELETE SET NULL` — связь повисает: `target_id = NULL`, `target_name` остаётся | миграция 005 |
| GC-ретракт superseded | Сначала SET NULL у целей, затем зачистка повисших | `gc_superseded` + `orphans_cleanup` ([service.py:667](../memory_server/memory/service.py)) |
| `orphans_cleanup` | Удаляет связи, у которых нет ни `target_id`, ни `target_name`. Идемпотентно | `DELETE_ORPHAN_RELATIONS` |

---

## 2. Правило владения: кто кого трогает

Ключевой инвариант системы. Владелец связи определяется пометкой в `metadata`:

| Владелец | Признак | Кто создаёт | Кто удаляет/меняет |
|---|---|---|---|
| Синк метаданных | `metadata->>'synced_from' = 'metadata.links'` | `sync_links_batch` | Только синк (при каждом прогоне перезаписывает) |
| Ручные | Пометки нет | `memory_link` | Только клиент (`memory_unlink`); синк их **не трогает** |
| auto_linker (план) | Своя пометка, напр. `synced_from='auto_linker'` | OrphanLinker | Только линкер и reconciler |

Следствия:

- Ручная связь между теми же гранулами и с тем же типом, что и синковая, не конфликтует:
  unique-индекс `(source_id, target_id, link_type) WHERE target_id IS NOT NULL`
  схлопнет их в одно ребро при повторном синке, но до этого живут оба — читайте `direction`.
- Никогда не редактируйте синковые связи руками — следующий синк откатит правку.
  Меняйте источник (`metadata.links`).

---

## 3. Резолвинг target: UUID vs entity_name

Колонки `target_id` (жёсткая ссылка) и `target_name` (мягкая, entity_name) —
взаимоисключающие: заполнена ровно одна.

| Ситуация | Результат синка | Читается? |
|---|---|---|
| `target` = UUID существующей гранулы | `target_id` заполнен — полноценное ребро | Обход графа, incoming |
| `target` = UUID несуществующей гранулы | Связь отброшена (`EXISTS`-фильтр) | Нет |
| `target` = имя (entity_name) | `target_name` заполнен, `target_id = NULL` — **сирота** | Только outgoing; в обходе не участвует (`WHERE target_id IS NOT NULL` в `TRAVERSE_CTE`) |

**Текущая дыра (Г1):** regex в `SYNC_LINKS_BATCH`/`BACKFILL_RELATIONS_FROM_METADATA`
принимает только UUID. Тишь пишет в `metadata.links` и имена — всё именное оседает
сиротами, синк их резолвить не умеет (см. статистику в § 6).

**Целевая схема v2:** lateral-резолв entity_name → UUID прямо в SQL синка +
`resolve_interactive` + reconciler (сверка target_name с реальным содержимым цели).
Подробно — [GRAPH_LINKS_V2_ARCHITECTURE.md](GRAPH_LINKS_V2_ARCHITECTURE.md), § «Пайплайн v2».

---

## 4. Типы связей

Канонический список — **32 типа**, продублирован на трёх уровнях (менять — во всех трёх):

| Уровень | Где | Роль |
|---|---|---|
| Python | `LinkType = Literal[...]` — [models.py:11](../memory_server/models.py) | Pydantic-валидация на входе (ValidationError вместо PG CHECK в глубине воркера) |
| БД | `CONSTRAINT chk_link_type CHECK` — [миграция 005](../migrations/005_relations.sql) | Последняя линия обороны |
| MCP-схема | Описание инструмента `memory_link` | Подсказка агенту |

### Матрица по назначению (cross-namespace matrix)

| Группа | Типы | Семантика |
|---|---|---|
| Кодовые (8) | `depends_on`, `used_by`, `extends`, `implements`, `contains`, `contained_by`, `calls`, `called_by` | Рёбра между кодовыми гранулами |
| Общие (11) | `related_to`, `contradicts`, `solves`, `tested_by`, `implements_adr`, `references`, `follows`, `precedes`, `alternative_to`, `causes`, `prevents` | Универсальные и логические |
| Инфраструктурные (3) | `runs_on`, `exposes`, `mounts` | Гранула → сервер/порт/том |
| Cross-namespace (5) | `derived_from`, `motivates`, `informs`, `informed_by`, `connected_to` | Связи между слоями (код ↔ решение ↔ диалог) |
| Supersession и кластеры (5) | `supersedes`, `supports`, `member_of`, `part_of`, `describes_cluster` | Миграция 018: версии фактов и иерархии |

Направленность: связь читается как `source → target` («source depends_on target»).
`memory_get_relations` возвращает обе стороны — `outgoing` и `incoming`.

### Anti-inverse инвариант (план)

Сейчас БД не проверяет согласованность парных типов: можно создать
`A depends_on B`, не создав `B used_by A`, или, хуже, оба в «неправильные» стороны.
План v2 — инвариант на уровне аппликативного слоя: пары-инверсы
(`depends_on`↔`used_by`, `contains`↔`contained_by`, `calls`↔`called_by`,
`follows`↔`precedes`, `informs`↔`informed_by`) либо создаются вместе,
либо проверяются reconciler'ом. Детали — [GRAPH_LINKS_V2_ARCHITECTURE.md](GRAPH_LINKS_V2_ARCHITECTURE.md).

---

## 5. Вес связи = confidence

Колонка `weight` — трактуется как доверие к связи, 0..1.

**Целевая формула (v2):**

```
weight = P_source × S_sem × F_status × B_confirm
```

| Множитель | Что учитывает | Пример |
|---|---|---|
| `P_source` | Автор связи | Гранулятор Тиши `0.9` / агент `1.0` / auto_linker `0.6` |
| `S_sem` | Уверенность семантического сопоставления (резолв имён, похожесть) | точный UUID `1.0`, fuzzy-резолв `0.7` |
| `F_status` | Статус источника на момент связи | `asserted` `1.0`, `superseded` `0.3` |
| `B_confirm` | Подтверждённость (второе независимое упоминание, ручное подтверждение) | `1.0` / ниже |

**Текущее состояние:** синк пишет `weight = 1.0` безусловно, ручные `memory_link`
берут значение клиента (дефолт `1.0`). Формула — фаза плана v2; до её внедрения
вес в БД не различает источники связей. См. [GRAPH_LINKS_V2_ARCHITECTURE.md](GRAPH_LINKS_V2_ARCHITECTURE.md).

В поиске вес связи участвует опосредованно (rrf × recency_decay × importance —
это про гранулы, не про рёбра); рёбра пока используются для навигации по графу,
не для ранжирования.

---

## 6. Сироты

**Определение.** Сирота — связь с `target_id IS NULL` (цель задана именем и не
резолвнута) либо повисшая после `ON DELETE SET NULL`. Реже — безымянная
(`target_id IS NULL AND target_name IS NULL`), её чистит `orphans_cleanup`.

**Масштаб проблемы:** ~4.1k сирот из 14.7k связей (**~28 %**) на 2026-09-19 —
прямое следствие дыры Г1 (§ 3): всё именное из `metadata.links` ложится сиротами.

**План лечения (фазы плана v2):**

| Механизм | Что делает |
|---|---|
| Lateral-резолв в синке | Имена резолвятся в UUID прямо при `INSERT`, сироты перестают рождаться |
| OrphanLinker | Фоновый воркер: доresoлв существующих сирот (target_name → candidate по embedding + точное совпадение), пишет `synced_from='auto_linker'` |
| resolve_interactive | Интерактивный резолв неоднозначных имён с подтверждением человеком |
| Reconciler | Периодическая сверка `target_name` с содержимым цели, чистка ложных резолвов |
| GC-критерии | Сироты без шанса на резолв (имя не найдено ни одним способом) — удаление по возрасту/попыткам |

---

## 7. Связи vs кластеры Level 2 (миграция 022)

Кластеры — **не рёбра**. Это отдельный механизм Level 2, и смешивать его с графом
связей не нужно:

| | Связи (Level 1) | Кластеры (Level 2) |
|---|---|---|
| Носитель | Таблица `relations` — явные направленные рёбра | Таблица `clusters` + колонка `memories.cluster_id` |
| Кто создаёт | Тишь / клиент / синк / (будущий) auto_linker | Beat: `assign_clusters_from_pairs` (кандидаты — Qdrant ANN) |
| Семантика | «Кто на кого ссылается» — типизированная | «Похожи по смыслу» — тип не хранится |
| Схлопывание | Нет — ребро есть ребро | Порог близости (`cluster_threshold`), группы по namespace |
| Связь с кластером | `describes_cluster` — ребро «гранула описывает кластер», если понадобится | — |

Цитата из миграции 022: *«relation member_of для кластеров НЕ используем»* —
принадлежность к кластеру живёт в `cluster_id`, а не в графе. Единственный
мост между механизмами — тип `describes_cluster` (гранула-документ описывает
кластер-тему) и метрика `coherence` кластера (средняя косинусная близость по
обнаруженным рёбрам группы; низкая — сигнал рыхлости).

Пересчёт: `refresh_clusters(namespace)` — [service.py:675](../memory_server/memory/service.py);
graceful-отказы (`migration 022 pending`, `qdrant_unavailable`) не ломают beat-расписание.

---

## Шпаргалка: где что в коде

| Что | Где |
|---|---|
| Модель `Relation`, `LinkType` (32 типа) | [memory_server/models.py](../memory_server/models.py) |
| Схема таблицы, FK-поведение, CHECK | [migrations/005_relations.sql](../migrations/005_relations.sql) |
| `SYNC_LINKS_BATCH`, `DELETE_SYNCED_RELATIONS`, `DELETE_ORPHAN_RELATIONS` | [memory_server/db/queries.py](../memory_server/db/queries.py) |
| `get_relations_unified`, `graph_traverse_full`, `graph_stats_unified` | [014_stored_procedures_optimizations.sql](../migrations/014_stored_procedures_optimizations.sql), [020_stored_procedures_canonical.sql](../migrations/020_stored_procedures_canonical.sql) |
| Точки синка (store/update/batch) | [service.py:204,408](../memory_server/memory/service.py), [memory_tasks.py:729](../memory_server/tasks/memory_tasks.py) |
| Кластеры | [migrations/022_clusters.sql](../migrations/022_clusters.sql) |
| План Web UI фаза 2 (сироты в UI, v2-механика) | [PLAN_WEB_UI_PHASE2.md](PLAN_WEB_UI_PHASE2.md), § 1 |
| Архитектура v2 (Г1–Г3, L1–L4, confidence) | [GRAPH_LINKS_V2_ARCHITECTURE.md](GRAPH_LINKS_V2_ARCHITECTURE.md) |
