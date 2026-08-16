# План: Иерархическая семантическая память (Level 0–5)

> Версия: 0.1  
> Дата: 2026-08-16  
> Автор: Афина (по запросу Серёжи)  
> Статус: Draft  
> Связанные документы: plan-new-features.md, PLAN_pgvector_removal.md, MIGRATION_QDRANT.md, ADR по графу

## 1. Цель

Перейти от плоской модели памяти (гранулы + простой граф) к многоуровневой иерархической системе с:

- версионированием фактов (git-like supersession);
- явным статусом истины / уверенности;
- динамическими кластерами;
- схемами, инсайтами и теориями;
- сохранением обратной совместимости с текущим MCP API.

Текущий стек (PostgreSQL + Qdrant + Qwen3 Embeddings 8B через llama.cpp + Celery) остаётся без изменений.

## 2. Иерархия уровней

```
Level 0: Сырые данные (диалоги, логи, файлы кода)
    ↓ грануляция (существующий агент)
Level 1: Гранулы (атомарные факты)
    ↓ связи + clustering
Level 2: Кластеры (группы связанных фактов)
    ↓ обобщение (LLM)
Level 3: Схемы (знания о группах / инварианты)
    ↓ meta-анализ
Level 4: Инсайты (знания о знаниях)
    ↓ эволюция
Level 5: Теории (модели мира)
```

### 2.1 Level 1 — Гранулы (ядро)

Атомарная единица. Расширяем существующую модель `memories` / `MemoryRecord`.

Новые / обязательные поля:

| Поле              | Тип              | Описание |
|-------------------|------------------|----------|
| `valid_from`      | TIMESTAMPTZ      | Начало действия факта |
| `valid_to`        | TIMESTAMPTZ NULL | Конец действия (NULL = актуально) |
| `transaction_time`| TIMESTAMPTZ      | Когда запись попала в систему |
| `status`          | TEXT             | `asserted` \| `superseded` \| `retracted` \| `uncertain` |
| `confidence`      | FLOAT            | 0.0–1.0 |
| `belief`          | FLOAT (optional) | Субъективная уверенность агента |
| `supersedes`      | UUID NULL        | ID предыдущей версии |
| `superseded_by`   | UUID NULL        | ID следующей версии |
| `source_ref`      | TEXT / JSONB     | Ссылка на Level 0 (conversation_id, file, log) |
| `cluster_id`      | UUID NULL        | Принадлежность к кластеру |
| `importance`      | INT              | Уже есть |

Правило изменения факта («любимый цвет синий → зелёный»):

1. Старая гранула: `valid_to = now()`, `status = 'superseded'`, `superseded_by = new_id`.
2. Новая гранула: `valid_from = now()`, `status = 'asserted'`, `supersedes = old_id`, новый embedding в Qdrant.
3. В `relations` добавляется ребро `link_type = 'supersedes'`.

Поиск по умолчанию фильтрует `valid_to IS NULL AND status = 'asserted'`.

### 2.2 Level 2 — Кластеры

Динамические группы семантически близких гранул.

- Хранение: таблица `clusters` (id, centroid_payload, summary, member_count, created_at, updated_at, namespace).
- Связь гранула ↔ кластер: relation `member_of` или поле `cluster_id`.
- Алгоритм: периодический HDBSCAN / hierarchical clustering по embeddings из Qdrant (scroll + distance) или on-demand при изменении кластера > N%.
- Реализация: Celery task `cluster_rebuild` / `cluster_update`.

### 2.3 Level 3 — Схемы

Обобщение кластера (инварианты, краткое описание группы).

- Хранятся как обычные гранулы в namespace `schemas`.
- Связь: relation `describes_cluster` → cluster_id.
- Генерация: LLM-агент по триггеру «кластер существенно изменился» или по расписанию.

### 2.4 Level 4 — Инсайты

Мета-знания («предпочтения пользователя по цвету меняются раз в 3–6 месяцев»).

- Namespace `insights`.
- Высокий `importance`.
- Связи `derived_from` на схемы / кластеры / гранулы.
- Генерация — редкий meta-агент.

### 2.5 Level 5 — Теории

Модели мира. Создаются редко, явно или очень тяжёлым meta-процессом. Версионируются так же, как гранулы.

## 3. Истина, противоречия, уверенность

Не бинарный true/false.

- `confidence` + `evidence_count` + `contradiction_count`.
- При появлении противоречащей гранулы:
  - создаётся relation `contradicts`;
  - понижается confidence обеих;
  - опционально LLM предлагает resolution (оставить одну, создать контекстно-зависимую версию и т.д.).
- Фоновый decay: старые неподтверждённые факты постепенно теряют confidence.

## 4. Граф связей

Расширить существующую таблицу `relations` новыми `link_type`:

- `supersedes`
- `contradicts`
- `supports`
- `member_of`
- `derived_from`
- `part_of`
- `related_to`
- `caused_by`
- `describes_cluster`

Существующие tools (`memory_link`, `memory_unlink`, `memory_get_relations`, `memory_traverse`, `memory_graph_stats`) остаются. При необходимости добавить materialised path / closure table позже.

## 5. Изменения в схеме БД (миграции)

Рекомендуемый порядок:

1. **Миграция bitemporal + status** (на базе уже запланированной 019):
   ```sql
   ALTER TABLE memories
     ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT now(),
     ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ,
     ADD COLUMN IF NOT EXISTS transaction_time TIMESTAMPTZ DEFAULT now(),
     ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'asserted',
     ADD COLUMN IF NOT EXISTS confidence REAL DEFAULT 0.8,
     ADD COLUMN IF NOT EXISTS supersedes UUID REFERENCES memories(id),
     ADD COLUMN IF NOT EXISTS superseded_by UUID REFERENCES memories(id),
     ADD COLUMN IF NOT EXISTS cluster_id UUID,
     ADD COLUMN IF NOT EXISTS source_ref JSONB;

   CREATE INDEX IF NOT EXISTS idx_memories_valid ON memories(valid_from, valid_to);
   CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status) WHERE valid_to IS NULL;
   CREATE INDEX IF NOT EXISTS idx_memories_cluster ON memories(cluster_id);
   ```

2. Таблица `clusters`:
   ```sql
   CREATE TABLE IF NOT EXISTS clusters (
     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     namespace TEXT NOT NULL DEFAULT 'default',
     summary TEXT,
     centroid_id UUID,
     member_count INT DEFAULT 0,
     metadata JSONB DEFAULT '{}',
     created_at TIMESTAMPTZ DEFAULT now(),
     updated_at TIMESTAMPTZ DEFAULT now()
   );
   ```

3. При необходимости — небольшие расширения `relations`.

Qdrant payload должен дублировать ключевые фильтры: `status`, `valid_to` (или флаг `is_current`), `confidence`, `cluster_id`, `namespace`.

## 6. Изменения в коде

### 6.1 Models (`memory_server/models.py`)

- Расширить `MemoryRecord` / `MemoryInput` новыми полями.
- Добавить модели `Cluster`, `ClusterCreate` и т.д.

### 6.2 Service / Repository

- `store` / `ingest_batch`: поддержка `supersedes`, автоматическая простановка `valid_to` у старой версии.
- `search` / `find_similar`: по умолчанию фильтр `status = 'asserted' AND valid_to IS NULL`.
- Новые методы: `get_history(memory_id)`, `get_current(memory_id)`, `create_version(...)`.
- Clustering tasks в Celery.

### 6.3 MCP Tools

Обратная совместимость сохраняется. Новые/расширенные:

- `memory_store` — принимает опциональные `supersedes`, `confidence`, `status`.
- `memory_search` — параметр `include_historical: bool = False`.
- `memory_get_history(id)`
- `memory_cluster_list` / `memory_cluster_get` / `memory_cluster_rebuild` (позже)
- `memory_create_schema` / `memory_create_insight` (позже)

### 6.4 Агент грануляции

Расширить промпт и логику:

- детектировать потенциальную supersession / contradiction;
- предлагать `supersedes` и связи;
- при необходимости триггерить re-clustering.

## 7. Фоновые процессы (Celery)

| Task                    | Триггер                  | Описание |
|-------------------------|--------------------------|----------|
| `cluster_rebuild`       | cron / threshold         | Пересчёт кластеров |
| `confidence_decay`      | daily                    | Понижение confidence старых неподтверждённых |
| `schema_generate`       | изменение кластера       | LLM-генерация схемы |
| `insight_generate`      | редкий / ручной          | Meta-анализ |
| `gc_superseded`         | weekly                   | Архивация очень старых superseded-версий |

## 8. Этапы внедрения

### Фаза A — Фундамент (1–2 дня)
- Миграция bitemporal + status + supersedes.
- Обновление models / repository / service.
- Фильтр «только актуальные» в search.
- Тесты на create_version / get_history.

### Фаза B — Граф и противоречия (1 день)
- Новые link_type.
- Логика contradicts + понижение confidence.
- Расширение агента грануляции.

### Фаза C — Кластеры (2–3 дня)
- Таблица clusters.
- Celery task clustering.
- Tools для просмотра кластеров.
- Базовый summary кластера.

### Фаза D — Схемы и инсайты (2–4 дня)
- Namespace `schemas` / `insights`.
- LLM-пайплайны генерации.
- Связи derived_from / describes_cluster.

### Фаза E — Теории + полировка
- Level 5.
- Мониторинг, метрики, документация.
- Настройка decay и GC.

## 9. Риски и митигации

| Риск | Митигация |
|------|-----------|
| Ломается обратная совместимость search | По умолчанию фильтр «актуальные», параметр `include_historical` |
| Рост объёма БД из-за версий | GC + archive superseded, Qdrant хранит только current |
| Качество clustering | Начинать с простых threshold + ручной review, потом HDBSCAN |
| Галлюцинации LLM при схемах/инсайтах | Высокий порог confidence, human-in-the-loop на первых этапах |
| Нагрузка на 3060Ti | Clustering и meta-агенты — в off-peak, batch |

## 10. Метрики успеха

- Доля гранул со status=asserted и корректным supersedes.
- Latency search (не должна вырасти заметно).
- Количество автоматически обнаруженных contradictions.
- Полезность кластеров/схем (субъективно + hit-rate при retrieval).

## 11. Следующие шаги

1. Ревью этого плана.
2. Написание миграции (Фаза A).
3. Обновление models.py и service layer.
4. Расширение промпта агента грануляции.
5. После стабилизации Level 1–2 — переход к схемам.

---

Документ можно дополнять по мере реализации. Все изменения в коде — только после явного согласования.
