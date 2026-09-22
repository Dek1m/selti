# OBS BASELINE V3.5 — пред-деплойное состояние (волна 2: Ф1 жизнь рёбер + Ф2 PPR traverse)

**Снято:** Мая (observability), 2026-09-22 ~21:53 UTC
**Метод:** MCP-инструменты чтения (memory_linker_stats / memory_graph_stats / memory_stats) + память проекта. Прод напрямую не трогался.
**Назначение:** точка сравнения после деплоя V3.5 (миграция 026_edge_lifecycle + код Ф1/Ф2).

## 1. Статистика линкера (memory_linker_stats)

| Показатель | Значение |
|---|---|
| links_total | **81 145** |
| — l1a (synonym ANN) | 8 |
| — l1c (co-occurrence) | 81 132 |
| — l2 (LLM-вердикт) | 5 |
| names_resolved | 9 659 |
| names_pending | 2 171 |
| l2_enabled / l2_mode | false / manual |
| Вердикты (ручные) | link=5, остальные 0 |
| verdict_cache | hits=0, misses=0 |
| l2_queue_size | 0 |

## 2. Граф знаний (memory_graph_stats)

| Показатель | Значение |
|---|---|
| total_granules | **15 519** |
| total_relations | **106 557** |
| linked_granules | 14 398 |
| orphans | 1 121 |
| avg_connections | 13.73 |
| Топ link-типы | related_to 86 458, references 11 106, implements_adr 2 668, solves 1 536, depends_on 976 |

**Рёбра active/pruned:** до деплоя все 106 557 — active; pruned=0 (колонка pruned_at появляется с миграцией 026). После деплоя отслеживаем сдвиг active→pruned по кампаниям edge_prune (сначала dry).

## 3. Память по namespace (memory_stats, project=selti)

| Namespace | Записей | last_updated (UTC) |
|---|---|---|
| code_knowledge | 899 | 2026-09-22T21:53:04Z |
| dialogue_insights | 234 | 2026-09-22T21:53:04Z |
| infrastructure | 84 | 2026-09-22T21:53:04Z |
| project_meta | 415 | 2026-09-22T21:53:04Z |
| user_facts | 5 | 2026-09-22T20:23:51Z |

(незавиcимые счётчики: code_index-гранулы в code_knowledge — основная масса из 8 565 гранул этого namespace в графе)

## 4. Латентности (последние известные; прямого доступа к прод-метрикам нет)

| Операция | Baseline | Источник |
|---|---|---|
| memory_search (MCP) | p50 ≈ 0.5–1.6 c, embedding-dominated | координатор/память проекта |
| MCP tool duration (общий) | прод-выбросы p95 в районе 0.8–2.0 c | калибровка бакетов MCP_TOOL_DURATION_SECONDS (Рэй) |
| traverse (BFS) | hard-cap 500 узлов (traverse_max_nodes) | конфиг |
| traverse (activation/PPR) | нет данных — стратегия выключена (traverse_activation_enabled=False) | config.py |
| edge_prune кампания | нет данных — задача новая, первая кампания dry (edge_prune_dry_run=True) | config.py |

## 5. Конфигурационные флаги до деплоя (config.py)

| Флаг | Значение | Смысл |
|---|---|---|
| edge_lifecycle_enabled | **False** | Ф1 целиком выключена до команды |
| edge_prune_dry_run | **True** | первая кампания — только отчёт |
| edge_prune_min_age_days | 30 | возраст ребра-кандидата |
| edge_decay_floor | 0.05 | порог raw w_eff |
| edge_reinforcement_enabled | True | касания при dispatch (под гашением master-флагом) |
| traverse_activation_enabled | **False** | Ф2 выключена до включения |

## 6. Что отслеживаем после деплоя

1. **Prune forecast (dry):** `selti_edge_prune_candidates_total{mode="dry"}` и `selti_edge_prune_duration_seconds{mode="dry"}` — сколько рёбер наберёт порог w_eff ≤ 0.05 при возрасте ≥ 30д; кампания beat 03:30 UTC. Резкий рост кандидатов между ночами = затухание ест живой граф.
2. **Бой (live):** `selti_edge_pruned_total{mode="live"}` против предшествующего dry-forecast — расхождение больше шумового = поменялись данные между прогонами; restore-поток: `selti_edge_restore_total{result="ok"|"noop"}`.
3. **Reinforce:** `selti_edge_reinforced_total` — темп касаний (search/traverse-нагрузка); обвал к нулю при живом трафике = отвалился dispatch/очередь memory.
4. **PPR traverse:** `selti_traverse_activation_requests_total{status="ok"|"empty"}` + `selti_traverse_activation_latency_seconds` — сравнить с BFS-латентностью до деплоя; при негативе — откат флага traverse_activation_enabled (метрика исчезает из /metrics).
5. **Счётчик рёбер active/pruned:** после миграции 026 — периодический замер из БД (active = pruned_at IS NULL), ожидаемое начало: 106 557 / 0.
6. **Латентность search/traverse до/после:** не должна деградировать — Ф1 touch-путь асинхронный (очередь memory), Ф2 не в default-пути (bfs остаётся дефолтом).
7. **Linker-статы (раздел 1):** links_total по слоям не должен проседать — pruned l1c-рёбра влияют на память-объём L2-очереди и names_pending.

## 7. Алерты после стабилизации (предложение)

| Алерт | Уровень | Условие |
|---|---|---|
| Prune-бой съедает граф | warning | rate(selti_edge_pruned_total[1d]) > 2% от active за сутки |
| Reinforce встал | warning | increase(selti_edge_reinforced_total[1h]) == 0 при ненулевом трафике search |
| PPR latency | warning | p95(selti_traverse_activation_latency_seconds) > 5 c за 15 м |
| Prune-кампания висит | critical | selti_edge_prune_duration_seconds{mode="live"} > 240 c (soft_time_limit задачи) |
