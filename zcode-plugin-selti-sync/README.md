# selti-sync — ZCode-плагин автозаписи проектов в selti

SessionStart-хук: при старте сессии в git-воркспейсе регистрирует проект в
реестре selti (`POST /projects/register`). Реестр питает «облачко знаний»
(глобальный хук `knowledge-cloud`), которое раньше приходило только
вручную зарегистрированным проектам.

Дизайн — ADR-018 (`docs/ADR-018-selti-sync.md`): идемпотентный POST без GET,
вся логика матчинга (slug / repo_url / local_path) на сервере, плагин
stateless. Node 18+, ноль зависимостей.

## Что делает

1. Читает `ZCODE_PROJECT_DIR` из stdin-события хука.
2. `git remote get-url origin` (таймаут 2с). Не-git папка или клон без
   origin → выход молча: реестр не замусоривается.
3. `POST {selti}/projects/register` с телом `{slug, name, kind: "code",
   local_path, repo_url}` (slug = basename папки, name = slug).
4. **201** → одна строка в контекст сессии: `selti: проект «albedo»
   зарегистрирован в реестре (slug albedo)`. Всё остальное — тишина.

## Ответы сервера

| Код | `status` | Значение | Хук |
|---|---|---|---|
| 201 | `created` | slug свободен — проект создан | строка в контекст сессии |
| 200 | `matched` | slug+repo_url уже в реестре, nothing to do (no-op не трогает `updated_at`) | молча |
| 200 | `path_updated` | обновлён `local_path` (переезд папки) либо путь перепривязан к своему slug | молча |
| 409 | `slug_conflict` | slug занят **другим** repo_url — нужен человек (автосуффиксы запрещены) | молча (случай виден в логах сервера) |
| 401 | — | неверный/отсутствующий `X-SELTI-KEY` | молча |
| 422 | — | невалидное тело (kind вне code/infra/domain/workspace/org) | молча |

## Установка

Локальный plugin-workspace (плагин живёт в репо selti, дистрибуция — через
git, marketplace-публикации нет):

- ZCode: Settings → Plugin Management → добавить локальный плагин из
  `E:\Projects\Python\selti\zcode-plugin-selti-sync` (либо прописать в
  workspace-конфиг `plugin-workspace`).
- Проверка: Settings → Plugin Management → selti-sync → хук SessionStart
  помечен runnable.

## Конфигурация

Переменные окружения (приоритет) или файлы в `~/.zcode/` — как у хука
`knowledge-cloud`, конфиг общий на хост:

| Источник | Значение | Файл-фолбэк |
|---|---|---|
| Адрес сервера selti | `SELTI_URL` | `~/.zcode/selti-url` |
| Ключ записи | `SELTI_API_KEY` | `~/.zcode/selti-key` |

Дефолт адреса — `http://localhost:8000`. Если на сервере задан
`SELTI_API_KEY` (env), каждый POST обязан нести заголовок `X-SELTI-KEY`;
пустой ключ на сервере — эндпоинт открыт (LAN-режим).

Файлы (если используются) — одна строка значения, читаются при каждом старте:

```
~/.zcode/selti-url   → http://10.0.0.51:8000
~/.zcode/selti-key   → <секрет>
```

## Тесты

- Логика нормализации — node-тест: `node --test tests/test_selti_sync_hook.mjs`
  (из корня репо selti).
- Ручные проверки (без прода): `SELTI_URL=http://127.0.0.1:9` → exit 0 молча;
  не-git папка → exit 0 молча.

## Известные ограничения

- Облачко знаний новому проекту приходит со **следующей** сессии
  (knowledge-cloud может прочитать реестр до нашей регистрации) — ADR-018,
  graceful degradation.
- Конфликт «две машины, один repo_url, разные local_path» на этом уровне
  не решается (последняя запись выигрывает) — матч облачка идёт по
  local_path конкретной машины.
