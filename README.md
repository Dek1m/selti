# selti

**S**tore · **E**volve · **L**ink · **T**hink · **I**ntegrate

Selti — семантическая память для AI-агентов: MCP-сервер, который хранит знания гранулами, следит за их жизненным циклом, связывает их в граф знаний, собирает в контекст и встраивается в инструменты разработчика. Пять букв названия — пять зон ответственности системы и оглавление этого README:

| Буква | Зона | Что внутри |
|-------|------|------------|
| **S** — Store | Гранулы | Запись с дедупликацией, namespaces, хеш-слой, реестр проектов |
| **E** — Evolve | Жизненный цикл | Supersession-версии, confidence decay, GC, beat-расписание |
| **L** — Link | Граф и кластеры | 32 типа связей, Линкер V3, кластеризация, activation-обход |
| **T** — Think | Инсайты и контекст | «Облачко знаний», гибридный поиск, полная карта 3D |
| **I** — Integrate | Интеграции | MCP, HTTP API, jev-слой, ZCode-плагин |

> **Примечание:** Проект разработан с применением технологий искусственного интеллекта в рамках рабочего процесса Argenta Team. Код прошёл рецензирование, тестирование и подготовлен к эксплуатации в production-среде.

---

## Store

Всё знание живёт в **гранулах** — атомарных записях с эмбеддингом, метаданными и связями. Хранилище трёхслойное: PostgreSQL держит записи, граф и версии; Qdrant — векторы; Redis — кеш эмбеддингов, очереди задач и рабочие снапшоты.

### Запись с дедупликацией

`memory_store` и `memory_ingest_batch` прогоняют каждую запись через двухуровневый `DedupEngine`:

1. **Exact match** — SHA256(content) по `content_hash` в пределах namespace: `user_facts` перезаписывается, остальные — skip.
2. **Semantic match** — векторный поиск в Qdrant; совпадение с `score >= threshold` — skip.

Пороги настраиваются per-namespace:

| Namespace | Порог |
|-----------|-------|
| `default` | 0.95 |
| `user_facts` | 0.90 |
| `code_knowledge` | 0.95 |
| `dialogue_insights` | 0.85 |
| `project_meta` | 0.90 |
| `infrastructure` | 0.95 |

### Namespaces

Логическая изоляция данных внутри одной базы: `default`, `user_facts`, `code_knowledge`, `dialogue_insights`, `project_meta`, `infrastructure`. Валидация на уровне тулов; `memory_namespaces` возвращает живой реестр.

### Хеш-слой

`hash_upsert` / `hash_get` / `hash_list` / `hash_delete` — детекция изменений внешних источников: храните хеш файла, сессии или документа и проверяйте, обрабатывали ли его уже.

### Реестр проектов

`POST /projects/register` — идемпотентная регистрация проекта (kind: `code`, `infra`, `domain`, `workspace`, `org`). Реестр питает «облачко знаний» и интеграции (см. Think и Integrate).

---

## Evolve

Знание не вечно — и selti обращается с этим честно. Контент гранулы **неизменяем**: любая правка создаёт новую версию, старая закрывается (статус `superseded`, окно валидности заканчивается моментом появления новой).

### Версии

- `memory_supersede` — новая версия гранулы: наследует владельца, namespace, метаданные и связи графа; confidence умножается на 0.9.
- `memory_get_history` — вся цепочка версий от старейшей к актуальной.
- `memory_freeze` — заморозить вечный факт: не затухает, не попадает под чистку.
- `memory_stale_list` — кандидаты на ревизию: просевший confidence и тишина дольше N дней.

### Beat-расписание

Celery beat ведёт жизнь памяти по расписанию (настраивается runtime-ключами `schedule.*`):

| Задача | Когда | Что делает |
|--------|-------|------------|
| `refresh_clusters` | ежедневно 02:00 | Пересчёт кластеров |
| `confidence_decay` | ежедневно 03:00 | Полураспад confidence: неактуальное затухает |
| `mark_stale` | ежедневно 04:00 | Пометка кандидатов на ревизию |
| `gc_superseded` | еженедельно, вс 05:00 | Сборка устаревших версий (dry-run → боевой режим) |
| `orphans_cleanup` | еженедельно, вс 05:30 | Чистка сирот графа |

### Жизненный цикл рёбер

Связи графа тоже стареют: эффективный вес ребра затухает вместе с уверенностью, выродившиеся рёбра помечаются `pruned_at` — не удаляются, а выходят из активного обхода. История сохраняется.

---

## Link

Гранулы сами по себе — заметки. Смысл появляется, когда они связаны.

### Граф знаний

32 типа связей, которыми агент размечает отношения: `related_to`, `follows`, `contradicts`, `supersedes` и другие. Тулы: `memory_link`, `memory_unlink`, `memory_get_relations`, `memory_traverse` (BFS или activation), `memory_graph_stats`.

### Линкер V3

Автолинкер связывает новые гранулы без ручного труда — трёхслойная пирамида с непересекающимися зонами cosine:

| Слой | Зона | Механика |
|------|------|----------|
| **L1a synonym** | [0.80, 0.85) | Автоматическое `related_to` с weight=score, без LLM |
| **L1c co-occurrence** | без cosine | Одна сессия + namespace → `related_to` 0.5 (с капом) |
| **L2 verdict** | [0.85, dedup) | Один LLM-вердикт на гранулу: link / duplicate / contradiction / none. Без LLM-ключа — manual-режим: очередь разбирает человек-агент тулами `memory_linker_review` / `memory_linker_verdict` |
| **dedup-зона** | ≥ порога ns | Территория DedupEngine — линкер не ходит |

Результаты L2 кешируются в Redis (TTL 30 дней, инвалидация при правке гранулы). Отложенные висяки имён разгребает beat-кампания `name_reconciler`.

### Кластеры

Ночной `assign_clusters` группирует схожие гранулы в тематические кластеры (миграция 022); `memory_cluster_list` отдаёт обзор. Кластеры питают карту и контекст.

### Ассоциативный поиск

`memory_search(strategy="activation")` — персонализированный PageRank поверх графа: seed-хиты гибридного поиска активируют соседей, и в выдачу попадает смежное знание, которого нет в формулировке запроса.

---

## Think

Хранить и связывать — средство. Цель — отдать агенту готовый контекст.

### «Облачко знаний»

`memory_context(project)` собирает снапшот проекта: стек, решения, код, инсайты, инфраструктуру — выжимку из топ-гранул с полураспадом важности 30 дней (свежие решения всплывают над древними). Байт-в-байт тот же снапшот отдаёт `GET /context/{slug}`, digest — укороченная версия. Кеш в Redis, пересборка по dirty-флагу от beat.

### Поиск

`memory_search` — гибрид dense-векторов (Qdrant) и полнотекстового поиска (PostgreSQL), слитых через RSF-фьюжн с затуханием по времени и важностью гранулы. Хватает seed'ов — расширяйте activation'ом (см. Link).

### Полная карта 3D

Веб-карта показывает всю память как галактику: гранулы-звёзды в кластерах, рёбра — свет связи. Раскладка DrL (igraph, dim=3) строится воркером, gz-снапшот кешируется в Redis, `/api/map/full` отдаёт его за миллисекунды. Синаптические импульсы бегут по живым связям — видно, где память дышит.

---

## Integrate

Память полезна, только если она под рукой у агента.

### MCP-сервер

34 инструмента по MCP (Streamable HTTP, stateless, mount `/mcp`) — полный слой Store/Evolve/Link/Think из этого README. Каждый тул инструментирован метриками вызовов и длительности.

### jev — System One reflex

`jev_decide` / `jev_judge` / `jev_rate` — калиброванные решения без генерации текста: выбор из вариантов, да/нет-гейт, порядковая оценка. Быстрый слой для повторяющихся решений агентов (триаж, severity, гейты).

### HTTP API

REST-слой для веба и скриптов: `/api/search`, `/api/memories/{id}` (+ relations, similar), `/api/graph/{id}`, `/api/stats`, `/api/namespaces`, `/api/linker/stats`, `/api/map/meta|full`, `/api/settings`, `/api/contexts/{slug}`, `/projects`. Аутентификация — Bearer `API_KEY` (пустой — открытый LAN-режим); реестр проектов принимает `X-SELTI-KEY`.

### ZCode-плагин selti-sync

SessionStart-хук: при старте сессии в git-воркспейсе тихо регистрирует проект в реестре (`POST /projects/register`, идемпотентно, stateless, ноль зависимостей). Живёт в `zcode-plugin-selti-sync/`, дизайн — ADR-018.

### Веб-интерфейс

`/ui` — React 19 + Three.js: полная карта 3D, 2D-граф (Sigma.js + ForceAtlas2), экраны Graph / Projects / Search / Stats, поиск по гранулам с lineage-цепочками версий. Экран `/ui/settings` управляет runtime-конфигурацией.

### Конфигурация — три слоя

Значение резолвится по приоритету: **env/compose** (явно заданный, применяется рестартом) → **БД `app_settings`** (меняется налету, hot-reload через `pg_notify`) → **дефолт из реестра** (`memory_server/config.py`). Env-ключ жёстко блокирует поле в UI (read-only, 409 на запись); секреты и фундамент (адреса, порты) в БД не попадают никогда. Полный контракт — [docs/SETTINGS.md](docs/SETTINGS.md), реестр 97 ключей — [docs/SETTINGS_REGISTRY.md](docs/SETTINGS_REGISTRY.md).

---

## Roadmap

Что дальше — по возрастанию амбиции:

### Tasks

Планировщик задач внутри selti: гант + канбан поверх реестра проектов. Проекты уже живут в системе — Tasks превратит их в рабочие доски со связями на гранулы-решения.

### Integrate — расширение

- **GitLab-интеграция** — автоматическая грануляция issues/MR/коммитов в память проекта.
- **ZCode-плагин вплетания контекста** — не только регистрация проекта, но и автоматическая вставка «облачка знаний» в системный промпт каждой сессии.

---

## Архитектура

```
Client (MCP over Streamable HTTP)        Web UI (/ui)
       │                                      │
       ▼                                      ▼
┌───────────────────── FastAPI ──────────────────────┐
│  /mcp (FastMCP, 34 tools)      /api/*  /ui  /tasks │
└──────────────────────────┬─────────────────────────┘
                           │ send_task
                           ▼
              ┌───── Redis (broker) ─────┐
              │  memory │ batch │ hash   │
              └──┬───────────┬───────┬───┘
                 ▼           ▼       ▼
         Celery Worker   Celery Beat  Flower (dev)
                 │
     ┌───────────┼───────────────┐
     ▼           ▼               ▼
PostgreSQL    Qdrant         Embedding API
(гранулы,     (векторы,      (OpenAI-совместимый,
 граф,        HNSW)           qwen3-embedding-8b)
 версии,
 app_settings)
```

### Стек

| Компонент | Технология |
|-----------|------------|
| Язык | Python 3.14 |
| MCP | FastMCP 3.x (Streamable HTTP) |
| API | FastAPI + Uvicorn |
| БД | PostgreSQL 18 (asyncpg) |
| Векторы | Qdrant 1.12 (HNSW, 4096-мерные эмбеддинги) |
| Кеш / брокер | Redis 7 |
| Фоновые задачи | Celery 5 (prefork) + beat + Flower |
| Web UI | React 19, Three.js, Sigma.js, Vite |
| Мониторинг | Prometheus + Grafana |

### Процессы

| Сервис | Роль |
|--------|------|
| `selti` | HTTP/MCP-сервер: приём запросов, постановка задач |
| `selti-worker` | Celery worker: вся тяжёлая работа — эмбеддинги, поиск, дедуп, линкер, карта |
| `selti-beat` | Планировщик: decay, GC, кластеры, reconciler |
| `flower` | Мониторинг воркеров (dev-профиль) |

**Почему Celery:** MCP-сервер только ставит задачи в очередь; prefork-воркеры изолируют блокирующие операции (embedding, векторный поиск) от event loop. Очереди: `memory` (240/300s), `batch` (600/900s), `hash` (120/180s). Retry — 5 попыток, exponential backoff + jitter; validation-ошибки не ретраятся.

---

## Быстрый старт

### Предварительные требования

- Docker 24+ и Docker Compose v2
- Python 3.14 (для миграций вне контейнера)

### Запуск

```bash
# 1. Клонировать репозиторий
git clone git@github.com:Dek1m/selti.git
cd selti

# 2. Скопировать шаблон окружения
cp .env.example .env

# 3. Сгенерировать пароли
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 4. Заполнить .env:
#    SELTI_DB_PASSWORD  — пароль БД приложения
#    POSTGRES_PASSWORD  — пароль суперпользователя PostgreSQL
#    EMBEDDING_API_URL / EMBEDDING_API_KEY — API эмбеддингов
#    QDRANT_URL — адрес Qdrant

# 5. Запустить стек (включая локальные postgres/qdrant/redis)
docker compose up -d

# 6. Проверить здоровье
curl http://localhost:8000/health

# 7. Применить миграции
python migrations/run.py
```

Для внешних PostgreSQL/Qdrant/Redis — уберите соответствующие сервисы и укажите URL в `.env`.

### Проверка MCP

```bash
# MCP endpoint отвечает на POST без handshake (stateless)
curl -X POST http://localhost:8000/mcp/ -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"ping","id":1}'
```

Веб-интерфейс: `http://localhost:8000/ui`

---

## Конфигурация

Система трёхслойная (env > БД > дефолты) — см. [Integrate](#integrate). Базовые переменные окружения:

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `DATABASE_URL` | PostgreSQL connection string (asyncpg) | `postgresql+asyncpg://...@localhost:5432/selti` |
| `QDRANT_URL` | Адрес Qdrant | `http://localhost:6333` |
| `QDRANT_COLLECTION` | Имя коллекции векторов | `memories` |
| `REDIS_URL` | Redis connection string | `redis://:@redis:6379/0` |
| `EMBEDDING_API_URL` | URL API эмбеддингов (OpenAI-совместимый) | `http://10.0.0.21:8080/v1` |
| `EMBEDDING_MODEL` | Модель эмбеддингов | `qwen3-embedding-8b` |
| `EMBEDDING_DIMENSION` | Размерность эмбеддинга | `4096` |
| `API_KEY` | Ключ MCP/HTTP-аутентификации (пусто — выключена) | (пусто) |
| `MCP_HOST` / `MCP_PORT` | Хост и порт сервера | `0.0.0.0` / `8000` |
| `SEARCH_DEFAULT_THRESHOLD` | Порог релевантности поиска *(runtime-ключ)* | `0.7` |
| `DEDUP_ENABLED` / `DEDUP_THRESHOLD` | Дедупликация: вкл. / глобальный порог *(runtime-ключи)* | `true` / `0.95` |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Брокер и бэкенд результатов | Redis |
| `CELERY_WORKER_CONCURRENCY` | Процессов на воркер | `4` |
| `CELERY_WORKER_MAX_MEMORY_PER_CHILD` | OOM-лимит на процесс (KB) | `200000` |
| `LOG_LEVEL` | Уровень логирования | `INFO` |

Runtime-ключи (`DEDUP_*`, `SEARCH_*`, `linker_*`, `schedule.*` и ещё ~90) переопределяются налету через `/ui/settings` без рестарта — см. [docs/SETTINGS_REGISTRY.md](docs/SETTINGS_REGISTRY.md).

---

## Мониторинг

| Сервис | URL | Назначение |
|--------|-----|------------|
| Health | `GET /health` | Статус сервера, БД, воркера |
| Metrics | `GET /metrics` | Prometheus: `selti_*` метрики |
| Tasks API | `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/cancel` | Жизненный цикл Celery-задач |
| Flower | `http://localhost:5555` (dev) | Визуальный мониторинг воркеров |

Метрики `selti_*` (префикс = `SERVICE_NAME`): HTTP-запросы и длительность, пул БД, вызовы эмбеддингов и попадания в кеш, результаты поиска и нулевые выдачи, вызовы MCP-тулов, дедупликация (skipped/inserted), счётчики и латентность Celery-задач, health-чеки.

Grafana-дашборд — `monitoring/dashboards/`, правила алертинга — `monitoring/alerts/prometheus-rules.yml` (доступность PG/Redis/Qdrant, длинные запросы, отсутствие бэкапов, память Redis). Экспортёры postgres/redis — `monitoring/exporters/`.

---

## Миграции

Версионированные SQL-файлы в `migrations/` (up/down), применяются встроенным runner'ом:

```bash
python migrations/run.py          # применить неприменённые
python migrations/run.py --down   # откатить последнюю
```

Актуальная схема — 27 миграций: гранулы и хеши (001–002), граф (005, 012), namespaces (006–007), переезд векторов на Qdrant и демонтаж pgvector (010–011), реестр проектов (017, 019), canonical-нормализация (018–020), фиксы поиска (021), кластеры (022), ядро Memory V3 (023), карта (024–025), жизненный цикл рёбер (026), runtime-настройки (027).

---

## Разработка

```bash
# Тесты (1260+)
pytest tests/ -v

# С покрытием
pytest tests/ --cov=memory_server -v

# Линтер
ruff check memory_server/ tests/
```

### Структура

```
memory_server/
├── tools/          # MCP-тулы: memory, hash, jev, context
├── api/            # HTTP-роутеры: web, settings, projects, context, tasks
├── memory/         # Ядро: service, dedup, linker, qdrant_store, map, search
├── tasks/          # Celery: memory, lifecycle, linker, map, hash, context
├── db/             # Пулы и схемы
├── embedding/      # Клиент эмбеддингов
├── server.py       # FastMCP + реестр тулов
├── celery_app.py   # Воркер + beat (RuntimeScheduler)
├── config.py       # Реестр настроек (дефолтный слой)
└── __main__.py     # FastAPI-сборка, uvicorn
web/                # React UI: карта 3D, граф, поиск, настройки
migrations/         # Версионированные SQL-миграции
zcode-plugin-selti-sync/  # ZCode-плагин автозаписи проектов
monitoring/         # Prometheus, Grafana, алерты, экспортёры
docs/               # ADR, планы, стандарты
```

---

## Команда

Проект разработан и сопровождается **Argenta Team**.

Разработчик: [Dek1m](https://github.com/Dek1m)

**Лицензия:** MIT
