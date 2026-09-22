# RUNBOOK: диагностические скрипты на проде selti (T0.4, план V3.5)

> Прод: **ai.atom.ui** (docker compose, проект `app`, сеть `app_default`).
> Контейнеры: `selti` (web), `selti-worker`, `selti-beat`, `postgres` (PG 18),
> `redis`, `athena-qdrant`, `nginx`.
> Правило T0.4: **диагностика строго read-only**. Никаких записей в боевые
> таблицы, никакого деплоя, никакого push.

Проверено Рэй 22.09: механика ниже прогнана на проде — read-only броня
отклоняет даже `CREATE TABLE` (см. «Верификация» в конце).

---

## 1. Доступ по SSH (с машины разработчика)

Ключ `~/.ssh/svc_athene_ai@atom.ui.key` запаролен; `ssh` в Windows-окружении
не вызывает askpass при подписи ключа сам — поэтому заходим через агента:

```bash
# Поднять агента, загрузить ключ (пассфразу отдаёт ~/.ssh/askpass.sh,
# на экран она не попадает), выполнить команду, убить агента.
eval "$(ssh-agent -s)" >/dev/null 2>&1
SSH_ASKPASS=/c/Users/User/.ssh/askpass.sh SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
  ssh-add /c/Users/User/.ssh/svc_athene_ai@atom.ui.key
ssh svc_athene_ai@ai.atom.ui '<команда>'
ssh-agent -k >/dev/null 2>&1
```

Прямой `ssh -o BatchMode=yes` без агента падает с
`Permission denied (publickey)` — это особенность вызова askpass, НЕ отказ
сервера: публичный ключ сервер принимает (`Server accepts key` в `ssh -v`).

`svc_athene_ai` входит в группу docker — `sudo` не нужен.

## 2. Окно диагностики (beat-кампании)

Расписание — `memory_server/celery_app.py` (`app.conf.beat_schedule`), UTC:

| Кампания | Когда | Вес |
|---|---|---|
| `refresh_clusters` | ежедневно 02:00 | тяжёлая |
| `galactic_layout` (layout-map) | ежедневно 02:30 | тяжёлая (~1 GiB RAM на пике) |
| `confidence_decay` | ежедневно 03:00 | тяжёлая |
| `mark_stale` | ежедневно 04:00 | средняя |
| `gc_superseded` | вс 05:00 | средняя |
| `orphans_cleanup` | вс 05:30 | средняя |
| `worker_stats.update` | каждые 30 с | лёгкая |
| `linker-l2-verdicts` | каждые 5 мин | лёгкая |
| `business_metrics`, `rebuild_contexts`, `linker-*` | раз в час | лёгкая |

**Подтверждено: после 05:30 UTC и до 02:00 UTC следующих суток тяжёлых
кампаний нет** — окно диагностики свободно. Диагностике не мешают и лёгкие
периодики (read-only SQL берёт только `ACCESS SHARE`).

Избегать: 02:00–05:30 UTC (05:00–08:30 МСК) — вершина жизненного цикла.

## 3. PostgreSQL: read-only исполнение SQL (скрипты Сони `*.sql`)

Подключение: контейнер `postgres`, роль `svc_athene_ai`, БД `memory`
(та же пара, что в auto-deploy.yml — принцип наименьших привилегий,
суперюзер `postgres` не используем).

### 3.1. Способ A — SQL-файл по stdin (рекомендуется: на сервере ничего не остаётся)

```bash
# Из каталога tools/diagnostics на машине разработчика:
( printf "%s\n" \
    "SET default_transaction_read_only = on;" \
    "SET statement_timeout = '30s';" \
    "SET idle_in_transaction_session_timeout = '60s';" \
    "\\pset pager off" \
  ; cat report.sql ) \
| ssh svc_athene_ai@ai.atom.ui \
    'docker exec -i postgres psql -U svc_athene_ai -d memory -v ON_ERROR_STOP=0'
```

- `docker exec` **обязательно с `-i`** (без него psql молча съест EOF и выйдет).
- `statement_timeout = '30s'` — именно в кавычках: `30s` без кавычек —
  синтаксическая ошибка (`trailing junk after numeric literal`, проверено).
- `default_transaction_read_only` и `statement_timeout` — сессионные GUC:
  `SET` из потока действует до конца сессии psql, транзакции не нужны.
- Результат — текст в stdout (таблицы psql). Для CSV:
  добавить `\pset format csv` в блок printf.

### 3.2. Способ B — интерактивная сессия (осмотр руками)

```bash
ssh -t svc_athene_ai@ai.atom.ui \
  'docker exec -it postgres psql -U svc_athene_ai -d memory \
     -c "SET default_transaction_read_only = on; SET statement_timeout = '\''30s'\'';" '
```

`SET` из `-c` живёт до конца сессии — дальше работаем, запись заблокирована.

### 3.3. Python-скрипты (`cosine_histogram.py`, `bridges.py`) — в контейнере `selti`

В контейнере `selti` уже есть драйверы и env: `DATABASE_URL`, `QDRANT_URL`,
`QDRANT_COLLECTION`, `QDRANT_ENABLED`, `REDIS_URL`.

```bash
# Прогон без копирования файла на сервер (stdin):
ssh svc_athene_ai@ai.atom.ui \
  'docker exec -i -e PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=30s" \
     selti python - ' < cosine_histogram.py
```

`PGOPTIONS` подхватывает libpq (psycopg2/psycopg3): скрипт Сони менять не надо.
Ограничение: **asyncpg игнорирует PGOPTIONS** — если скрипт на asyncpg,
read-only задаётся в коде: `connect(..., server_settings={
"default_transaction_read_only": "on", "statement_timeout": "30000"})`.
Такой скрипт на прод не запускать без этой обвязки.

С аргументами: перед `python -` дописать их после дефиса:
`docker exec -i selti python - --limit 1000 < script.py`.

## 4. Qdrant: только чтение (scroll/retrieve)

Контейнер `athena-qdrant`, порт 6333 (HTTP) / 6334 (gRPC), API-ключ НЕ
установлен. Коллекция: `memories`.

```bash
# С хоста:
curl -s http://localhost:6333/healthz                          # liveness
curl -s http://localhost:6333/collections                      # список
curl -s http://localhost:6333/collections/memories             | info о коллекции

# scroll — чтение точек (POST, но семантика read-only):
curl -s -X POST http://localhost:6333/collections/memories/points/scroll \
  -H 'Content-Type: application/json' \
  -d '{"limit": 10, "with_payload": true, "with_vector": false}'

# retrieve — чтение по id:
curl -s -X POST http://localhost:6333/collections/memories/points \
  -H 'Content-Type: application/json' \
  -d '{"ids": ["<uuid>"], "with_payload": true, "with_vector": false}'

# count:
curl -s -X POST http://localhost:6333/collections/memories/points/count \
  -H 'Content-Type: application/json' -d '{"exact": true}'
```

Из контейнера `selti` — по Docker DNS `http://athena-qdrant:6333`
(или значение `QDRANT_URL` из env контейнера).

**Белый список эндпоинтов** (всё остальное — запрещено):
`GET /healthz`, `GET /collections`, `GET /collections/memories`,
`POST .../points/scroll`, `POST .../points` (retrieve), `POST .../points/count`.

**Запрещено:** `upsert`, `delete`, `delete-vectors`, `create/update/drop
collection`, `PATCH`-мутации. `with_vector: false` по умолчанию — векторы
тяжёлые, тянем только когда нужны (cosine_histogram).

## 5. Прод-дампы (для стенда Катерины и репетиции миграции 026)

Бэкапы: `~/backups/memory_<YYYYMMDD_HHMMSS>_pre-deploy.sql` — снимаются
workflow `auto-deploy.yml` перед каждым деплоем; ретеншн — последние 10,
старше 14 дней удаляются.

**Актуально на 22.09 23:53 МСК:** свежайший
`~/backups/memory_20260922_232151_pre-deploy.sql` (57.7 МБ, 23:21 МСК сегодня)
— новый дамп не нужен. Сегодня было 10 деплоев (21:06–23:23 МСК), все сняли
свои pre-deploy дампы.

Забрать дамп на стенд:

```bash
scp svc_athene_ai@ai.atom.ui:~/backups/memory_20260922_232151_pre-deploy.sql .
```

Если дамп всё же нужен заново (штатная безопасная операция; вечер МСК ок,
диск: 162 G свободно, available RAM ~4.2 GiB):

```bash
ssh svc_athene_ai@ai.atom.ui \
  'BACKUP=~/backups/memory_$(date +%Y%m%d_%H%M%S)_diag.sql && \
   docker exec postgres pg_dump -U svc_athene_ai -d memory > "$BACKUP" && \
   test -s "$BACKUP" && ls -lh "$BACKUP"'
```

Каталог `~/backups` не входит в ретеншн-маску `*_pre-deploy.sql` — ручной
`*_diag.sql` workflow не удалит.

## 6. Запреты (T0.4)

- НИКАКИХ `INSERT/UPDATE/DELETE/CREATE/ALTER/DROP` по боевым таблицам.
  Броня `default_transaction_read_only=on` обязательна в каждой сессии —
  даже для «просто посмотреть».
- Не трогать: деплой, `docker compose up/build`, push в main, контейнеры
  worker/beat, nginx.
- Не выводить на экран: пароли, `~/app/.env`, значения `SELTI_DB_PASSWORD`,
  пассфразу ключа.
- Файлы на сервер не копировать (механика stdin в п. 3–4 закрывает вопрос);
  если иначе нельзя — только `~/diag/`, никогда `~/app/`.

## 7. Верификация механики (прогон Рэй 22.09, прод)

```
SET default_transaction_read_only = on;   → SET, current_setting: on
SELECT count(*) FROM memories;            → 15517
CREATE TABLE _ray_ro_armor_test(x int);   → ERROR: cannot execute CREATE TABLE
                                             in a read-only transaction
SET statement_timeout = 30s;              → ERROR: trailing junk after numeric
                                             literal (нужны кавычки '30s')
```

Броня подтверждена: сервер отклоняет DDL в read-only сессии. SELECT-путь жив.

## 8. Риски и особенности

- **nginx `unhealthy`** — известный ложный healthcheck (allow-list
  10.0.0.0/24 и http→https режут localhost-запросы). Прод-трафик не затронут.
  Не чинить в рамках T0.4.
- **Авто-деплой на push в main** — во время диагностики/репетиции никто не
  пушит в main: деплой пересоздаст контейнеры и может применить миграции.
  Сегодня (22.09) было 10 деплоев за вечер — координировать окно с Афиной.
- **Шторм дампов**: ретеншн «последние 10» при частых деплоях за evening
  вытесняет старые — если Катерине нужна точка «до V3.5», забрать дамп
  заранее и хранить на стенде.
- **asyncpg без server_settings** — обходит read-only броню (см. п. 3.3).
- **Qdrant без auth** (порт открыт наружу) — известный долг из аудита;
  внутри T0.4 ограничиваемся белым списком эндпоинтов.
