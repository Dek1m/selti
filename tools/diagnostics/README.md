# tools/diagnostics — диагностический пакет «Жизнь графа знаний» (V3.5)

Задачи T0.1–T0.3: read-only диагностики графа знаний (PG `relations` /
`memories.cluster_id` + Qdrant `memories`) перед настройкой decay рёбер.
Код пишется локально, **на проде исполняет Рэй** (окно после 05:30 UTC).

## Состав

| Артефакт | Задача | Что даёт |
|---|---|---|
| `sql/dead_edges_l1c.sql` | T0.1 | Профиль l1c-рёбер: возраст, вес, singleton-пары (кандидаты decay), топ-хабы l1c-степени, висячие target |
| `sql/degree_histogram.sql` | T0.1 | Перцентили/бакеты степеней, покрытие графа изолированными гранулами, топ-20 хабов с именами |
| `sql/edges_by_layer.sql` | T0.1 | Срез рёбер по `metadata->>'source'/'layer'` + контрольный баланс (сверка с `memory_graph_stats`) |
| `cosine_histogram.py` | T0.2 | Гистограмма pairwise-косинусов per-namespace (markdown + JSON): плотность зон линкера 0.80/0.85/0.90/0.95 |
| `bridges.py` | T0.3 | JSON межкластерных рёбер-мостов (иммунитет-список для decay); опция betweenness топ-degree подграфа |

Прод-механика исполнения (SSH, docker exec, Qdrant white-list, дампы) —
в `RUNBOOK_PROD.md` (T0.4, проверено Рэем на проде 22.09).

## Правила безопасности (обязательны)

1. **READ ONLY.** Каждый SQL-файл сам открывает `BEGIN TRANSACTION READ
   ONLY` и ставит `statement_timeout = '30s'`. Питон-скрипты ставят на
   PG-сессию `SET default_transaction_read_only = on` + тот же таймаут:
   случайная мутация упадёт с «read-only transaction», зависший запрос
   умрёт через 30 секунд. Никаких мутаций в коде нет и быть не должно.
2. **Прод-окно: после 05:30 UTC** (после ночного beat-цикла) — вместе с
   общей нагрузкой не складываться.
3. **Батчи последовательные** (Qdrant retrieve по 256, без параллелизма):
   диагностика не конкурирует с прод-поиском по HNSW.
4. **Секреты только в env.** В коде и отчётах нет DSN/ключей: JSON/md не
   содержат эндпоинтов. Не коммитить `.env`, выводить пароли в логи нельзя.
5. **Воспроизводимость:** seed сэмплирования фиксирован (дефолт
   `selti-diag-v3.5`) — два прогона с одним seed видят одни гранулы.

## Переменные окружения

| Переменная | Обязательна | Назначение |
|---|---|---|
| `SELTI_DIAG_PG_DSN` | да | DSN PostgreSQL, напр. `postgresql://svc:pass@host:5432/memory` (префикс `postgresql+asyncpg://` тоже принимается конвертацией) |
| `SELTI_DIAG_QDRANT_URL` | да (T0.2) | URL Qdrant, напр. `http://host:6333`; `:memory:` — локальная самопроверка |
| `SELTI_DIAG_QDRANT_COLLECTION` | нет | Коллекция (default: `memories`) |
| `SELTI_DIAG_QDRANT_API_KEY` | нет | Ключ Qdrant Cloud, если включён |

## Запуск на проде (Рэй)

```bash
# T0.1 — SQL-отчёты (каждый самостоятельный, порядок любой)
psql "$DSN" -f sql/dead_edges_l1c.sql   > reports/dead_edges_l1c.txt
psql "$DSN" -f sql/degree_histogram.sql > reports/degree_histogram.txt
psql "$DSN" -f sql/edges_by_layer.sql   > reports/edges_by_layer.txt

# T0.2 — косинус-гистограмма (сэмпл ≤3000 гранул на namespace)
export SELTI_DIAG_PG_DSN=... SELTI_DIAG_QDRANT_URL=...
python cosine_histogram.py --json out/cosine.json --markdown out/cosine.md

# T0.3 — межкластерные мосты (иммунитет-список для decay)
python bridges.py --json out/bridges.json
#    + междуность топ-5000 узлов (igraph, таймаут 60с):
python bridges.py --betweenness --json out/bridges_btw.json

# План без подключений (проверить, что подхватился env):
python cosine_histogram.py --dry-run
python bridges.py --dry-run --betweenness
```

Скрипты запускаются из каталога `tools/diagnostics` (`python
cosine_histogram.py`) либо как модуль из корня репо (`python -m
tools.diagnostics.cosine_histogram`).

## Формат выхода

- `cosine_histogram.py`: markdown (сводная таблица per-namespace + ASCII-
  гистограммы) и JSON (`namespaces.<uid>.stats`: `pairs`, `mean`,
  `median`, `p95`, `histogram` с бакетами 0.05, `shares` `>0.80..>0.95`;
  `missing_sync_pct` — доля сэмпла без вектора в Qdrant, метрика
  рассинхрона PG↔Qdrant).
- `bridges.py`: JSON `{generated_at, total_bridges, by_link_type,
  by_namespace_pair, bridges: [{source_id, target_id, link_type, weight,
  src_cluster, tgt_cluster, src_name, tgt_name, src_namespace,
  tgt_namespace, rel_source, layer, created_at}], betweenness?}`.

## Локальная самопроверка (без прода)

```bash
# из корня репо
python -m pytest tools/diagnostics/tests -q
```

Тесты закрывают чистую математику (косинус-матрица/гистограмма/доли на
синтетических векторах, включая нулевые и NaN-векторы), извлечение
мостов на синтетическом графе, betweenness на цепочке (центр = максимум)
и UAT-прогон пайплайна T0.2 на in-memory Qdrant с подсунутым сэмплом.
SQL-файлы прогонялись на одноразовой мини-базе PostgreSQL 18 с
миграциями и синтетическим графом (схема проверки — в комментариях
тестов `test_sql_selfcheck.py`).
