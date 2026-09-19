# PLAN_WEB_UI_PHASE2 — Веб-морда selti: вторая итерация

> Собран по замечаниям Мастера от 2026-09-19 (вечер) и наработкам Афина/Соны/Ирис.
> Статус: Draft → на декларацию Мастера. Базис: коммит 658f822.

## 0. Что уже живо (базис)

- https://ai.atom.ui/ui/ — LAN-only (allow 10.0.0.0/24), без логина; nginx инъектирует
  Bearer для LAN-клиентов (conf.d/zz-selti-api-auth.conf). Снаружи — Bearer.
- Поиск: hybrid, фильтры-чипы, пагинация по 20 (offset сквозь REST→task→service),
  карточка гранулы с ScoreGauge (rrf × decay × importance), lineage, relations, similar.
- Граф (sigma.js, 2D): запрос сеет узлы, топ-8 расширяется /relations, кап 120.
- Проекты: реестр + облачко знаний. Статистика: спектр слоёв + /health.
- REST: /api/search(offset), /api/memories/{id}(+history/relations/similar),
  /api/graph/{id}, /api/stats, /api/namespaces, /api/projects(CRUD), /api/contexts/{slug}.

## 1. Как устроены алгоритмы связей (документация — także docs/GRAPH_LINKS.md, фаза E)

**Рождение.** Связи пишет Тишь при грануляции: в `metadata.links` каждой гранулы —
массив `{type, target, description}`. `target` — либо полный UUID, либо entity_name
(строка). Ручные связи — тулом `memory_link` (пишет в `relations` напрямую).

**Синхронизация** (`sync_links_to_relations`, дергается на каждом store/update/ingest_batch,
non-fatal): DELETE связей с пометкой `synced_from='metadata.links'` (ручные не трогаются)
→ INSERT заново из `metadata.links` (BACKFILL_RELATIONS_FROM_METADATA). Дедуп по
(source_id, target_id, link_type). Правило владения: metadata.links = источник истины
для синхронизированных связей.

**Резолвинг цели.** `target` резолвится в UUID только если строка — валидный UUID;
иначе пишется в `target_name` как висячая связь по имени. Следствие: связи с
entity_name-целями НЕ соединяют узлы графа (граф строится по id) — часть «серости»
и пустоты именно отсюда.

**Чтение.** `get_relations_unified($id, $link_type)` — хранимка, UNION incoming/outgoing.

**Кластеризация (Level 2).** beat `refresh_clusters`: Qdrant ANN-соседи +
min-label propagation (миграция 022) — тематические кластеры, отдельные от связей.

**Сироты.** Гранула без единой связи в `relations`. Причины: (а) Тишь не всегда кладёт
metadata.links; (б) target по entity_name не резолвится в UUID; (в) сосед ещё не
существует на момент синка и не до-синкается потом. Замер 2026-09-19: ~4.1k сирот
из ~14.7k гранул (~28%).

## 2. Замечания Мастера (обязательные к реализации)

| # | Замечание | Решение |
|---|---|---|
| F1 | Плоский граф → **3D-паутина** как карта Eve Online, звёзды = гранулы | `3d-force-graph` (three.js + d3-force-3d), ленивый чанк; фазы D |
| F2 | Кнопка **«Открыть в Графе» ничего не делает** | Граф принимает seed `?id=` — строит созвездие от гранулы; чинится в фазе C/D |
| F3 | **Соседи цветных узлов — серые с коротким id**, должны быть цветными и разных размеров | Обогатить `/relations` полями соседа (namespace/entity_name/content/importance) — дизайн готов (фаза A) |
| F4 | **Firefox**: placeholder «Спроси глубину…» вылезает за узкое поле графа | `text-overflow: ellipsis` + укороченный placeholder/шрифт на узких инпутах; фаза C |
| F5 | **Поиск сбрасывается при переключении вкладок** (поиск/граф/проекты); граф тоже должен хранить состояние | Единый глобальный Zustand-стор с persist (sessionStorage); убрать wipe-hydrate на маунте; фаза B |
| F6 | Хочу понимать **алгоритмы связей** | docs/GRAPH_LINKS.md (раздел 1 этого плана → отдельный док); фаза E |
| F7 | **Сироты** — много, «избавляться вообще» | Политика: ре-линк кампания + точечный GC по критериям (фаза F, нужно решение Мастера) |

## 3. Фазы

### Фаза A — Контакт соседей: цветные и разные (0.5–1 день; Сона)
Бэкенд (аддитивно, без миграций):
- `models.py Relation`: `neighbor_namespace/neighbor_entity_name/neighbor_content/neighbor_importance` (optional).
- `queries.py`: `GET_NEIGHBORS_INFO` — `SELECT id, namespace, importance, metadata->>'entity_name', left(content,140) FROM memories WHERE id = ANY($1::uuid[])`.
- `pg_repository.get_relations`: собрать id соседей (target для outgoing, source для incoming), один батч-SELECT, разложить поля по связям.
Фронт:
- `graph.ts`: `ensureNode` берёт neighbor-поля → label (entity_name → content-голова → короткий id), namespace (цвет слоя), importance (размер).
- Проверка: серых сателлитов не остаётся; Firefox-скриншот дизайна.
Acceptance: в графе у ВСЕХ узлов цвет слоя и осмысленная подпись; размер = importance.

### Фаза B — Состояние переживает вкладки (0.5 дня; Сона)
- Один глобальный стор (`zustand/middleware persist`, sessionStorage): query/фильтры/страница поиска + query графа + id открытой карточки.
- `useUrlSync`: hydrate только если стор пуст (первый вход за сессию), иначе URL ← стор.
- Acceptance: набрал запрос → ушёл в Проекты → вернулся: запрос и страница на месте; граф хранит свой q.

### Фаза C — Багфиксы (0.5 дня; Сона)
- F2: GranulePanel «Открыть в Графе» → `/ui/graph?id=<uuid>`; GraphScreen: если есть `?id=` — fetch /api/memories/{id} (узел-центр) + /relations (окрестность), поиск-строку предзаполнить entity_name.
- F4: placeholder-переполнение в Firefox: `.searchbox input::placeholder { text-overflow: ellipsis; overflow: hidden }` + на графе placeholder короче («Запрос…») или font-size меньше; проверка в Firefox обязательна.
- Acceptance: кнопка ведёт в граф от этой гранулы; Firefox — ничего не вылезает.

### Фаза D — 3D-паутина (3–4 дня; Сона + Эна по архитектуре; на декларацию Мастера)
- Библиотека: `3d-force-graph` (three.js; sigma.js 3D не умеет). Ленивый чанк.
- Образ: чёрная глубина (--sl-bg-abyss), звёзды = гранулы — цвет слоя, радиус = importance, glow-спрайт = score; рёбра — тонкие линии с опциональным градиентом; туман/глубина для параллакса.
- Управление: орбит-камера (drag/zoom/pan), клик по звезде → GranulePanel, двойной клик → расширить окрестность узла (+1 hop), «Полёт к» из поиска.
- Данные: тот же seed-пайплайн (поиск → relations), кап по умолчанию 300–500 узлов; LOD/деградация: > 500 узлов — рёбра дальше 2 hop не рисуются.
- Переключатель 2D/3D на экране Графа (2D остаётся для слабых машин).
- Acceptance: 300 узлов — 60 fps на интегрированной графике; звёзды цветные/разных размеров; клик → карточка.
- Риск: WebGL в Firefox/IAB — проверить и fallback на 2D.

### Фаза E — Документация алгоритмов (0.5 дня; Тиамат)
- `docs/GRAPH_LINKS.md`: жизненный цикл связи (рождение → синк → чтение), роли metadata.links vs memory_link vs ручных, резолвинг target, сироты, кластеры Level 2. Источник раздела 1.

### Фаза F — Сироты: замер → ре-линк → GC (1–2 дня; Тишь + Нора; ПО РЕШЕНИЮ МАСТЕРА)
Мнение Афины: слепое удаление сирот — вредно (сирота ≠ мусор: это знание без связей).
1. **Замер**: график сирот по age/importance/access_count (graph_health + SQL).
2. **Ре-линк кампания** (Тишь): пройти сироты линк-обогащением — восстановить metadata.links из содержания и связать с существующими гранулами (semantic near-neighbors через Qdrant + подтверждение LLM). Цель: доля сирот < 10%.
3. **GC по критериям** (только после кампании; расширить lifecycle_tasks.orphans_cleanup или новый beat): удалять гранулу-сироту если ОДНОВРЕМЕННО age > 90д ∧ importance ≤ 2 ∧ access_count = 0 ∧ frozen = false ∧ нет кластера. dry_run месяц (как gc_superseded).
- Acceptance: доля сирот и «пустых» узлов в графе падает; ни одной удалённой «живой» гранулы.

### Фаза G — Контент реестра (0.5 дня; Тишь)
- Заполнить description/links 7 проектов через PATCH /api/projects/{slug} (микро-грануляция README).

## 4. Порядок и параллель
A → B → C — одним махом (Сона, ~2 дня). D — параллельно после A (Эна + Сона). E/G — Тиамат/Тишь в любой момент. F — старт после декларации Мастера по критериям GC.

## 5. Риски
| Риск | Митигация |
|---|---|
| 3D на 5k+ узлов не тянет | Кап 300–500, LOD, 2D-fallback, ленивый чанк |
| WebGL-нестабильность в Firefox/IAB | Проверка на двух браузерах; feature-detect → 2D |
| Ре-линк кампания «свяжет не то» | LLM-подтверждение каждой связи + выборка Мастером перед массовым применением |
| GC снесёт ценное | dry_run + 5 критериев одновременно + frozen-защита |
