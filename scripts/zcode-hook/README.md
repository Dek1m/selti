# ZCode-хук «Облачко знаний» (selti, Фаза 6.3)

`knowledge-cloud.mjs` — хук ZCode (Node 18+, zero-deps), два режима:

- **SessionStart** (`startup|resume`) — полный инжект: `~/.zcode/state.md`
  + секция агентов (запущенные задачи из metadata.json, с ма-а-аленьким
  контекстом работы) + облачко проекта (стек, решения, код, инсайты,
  инфраструктура). Digest-файл сохраняется — стартовая точка дельт.
- **UserPromptSubmit** — кеш-сохраняющая дельта (решение Мастера 19.09):
  лёгкий `GET /context/{slug}/digest` сверяется с digest-файлом.
  Не изменился → **exit 0 без вывода** (ноль токенов, префикс-кеш GLM
  дремлет). Изменился → инжект **только изменившихся секций**
  (append — ранее выданный текст никогда не переписывается).

Любой сбой (selti недоступен, нет env, таймаут 3с, 404) — хук молча
возвращает пустой `{"hookEventName":"SessionStart"}` и exit 0: сессия
стартует без задержек и без текста.

## Как работает

```
SessionStart
  └─ env ZCODE_PROJECT_DIR + ZCODE_SESSION_ID
       ├─ локально: ~/.zcode/cli/agents/sess_{SID}/agent_<id>/metadata.json
       │    → "▶ Сона (programmer) — Фаза 6: облачко [running, 25 мин]"
       │    (фоновые из чужих sess_* помечены 🌙; зомби >24ч отфильтрованы;
       │     кап 5 строк)
       └─ HTTP GET ${SELTI_URL}/projects   → матч local_path → slug
            └─ GET /context/{slug}?refresh=1  → свежий снапшот
                 └─ stdout additionalContext = state + агенты + облачко

UserPromptSubmit
  └─ GET /context/{slug}/digest (десятки байт)
       ├─ digest не изменился  → exit 0, тишина (кеш цел)
       └─ изменился            → GET /context/{slug} → diff секций
                                 → "## ☁ Обновление облачка: …" (append)
```

- `SELTI_URL` — базовый URL selti (default `http://localhost:8000`).
- Пути матчатся нормализованно: `\` ↔ `/`, без хвостового слэша,
  без учёта регистра.
- Digest-файлы: `~/.zcode/cli/agents/.cloud-digest-{slug}` —
  content-addressed точка сравнения (sha256 контента и секций).

## Включение

1. Сервер selti (с Фазой 6) доступен по `SELTI_URL`.

2. В `~/.zcode/cli/config.json` добавь блок `hooks`:

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "SessionStart": [
        {
          "matcher": "startup|resume",
          "hooks": [
            {
              "type": "process",
              "command": "node E:\\Projects\\Python\\selti\\scripts\\zcode-hook\\knowledge-cloud.mjs",
              "timeoutMs": 5000
            }
          ]
        }
      ],
      "UserPromptSubmit": [
        {
          "hooks": [
            {
              "type": "process",
              "command": "node E:\\Projects\\Python\\selti\\scripts\\zcode-hook\\knowledge-cloud.mjs",
              "timeoutMs": 5000
            }
          ]
        }
      ]
    }
  }
}
```

   Путь к скрипту — абсолютный. `timeoutMs: 5000` — бюджет хука; HTTP
   сам таймаутит за 3с. Для прод-сервера задай env `SELTI_URL` в команде
   (cross-platform: `node -e` обёртка не нужна — env можно задать через
   `cmd /c "set SELTI_URL=... && node ..."` на Windows или shell-строкой).

3. Перезапусти ZCode. Проверка вручную:

```bash
echo '{"hook_event_name":"SessionStart"}' | \
  ZCODE_PROJECT_DIR="E:\Projects\Python\selti" node scripts/zcode-hook/knowledge-cloud.mjs
# → {"hookEventName":"SessionStart","additionalContext":"## Агенты … ## Облачко знаний: selti …"}
```

В воркспейсе без записи в реестре (default-каталог) хук вернёт пустой
JSON — контекст не подкладывается (глобальный state виден в state.md
секции только при матче; для глобального слоя — Фаза 6.5).

## Ведение state.md

`~/.zcode/state.md` — 2 строки о текущем фокусе (что в работе / что
завершено). Ведёт Тишь (memory-granulator) в фоновых циклах; ручная
правка допустима:

```markdown
В работе: Фаза 6 — облачко знаний + ZCode-хуки.
Завершено: Фаза 3 — наблюдаемость (a71b459).
```

## Диагностика

- `GET ${SELTI_URL}/projects` — реестр: есть ли твой `local_path`?
- `GET ${SELTI_URL}/context/{slug}` — снапшот (404 до первого пересчёта;
  beat `rebuild_contexts` почасовой, `refresh=1` — немедленно).
- Digest-файл битый/устарел → удали `.cloud-digest-{slug}` — следующий
  UserPromptSubmit пересчитает дельту целиком.
- Хук всегда exit 0 — ошибки смотри в отсутствии `additionalContext`.
