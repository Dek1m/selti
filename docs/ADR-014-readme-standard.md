# ADR-014: Стандарт Argenta Team — Оформление репозиториев и README.md

**Статус:** proposed
**Дата:** 2026-08-01
**Автор:** Тиамат (Tech-Writer)
**Решение:** Единый стандарт оформления репозиториев и README.md для всех проектов Argenta Team

---

## Контекст

У проектов Argента Team нет единого стандарта оформления README.md. Анализ показал:

| Проект | Качество README | Проблемы |
|--------|----------------|----------|
| **selti** | 9/10 | Нет бейджа "Written by AI", нет секции команда |
| **akame** | 8/10 | Нет бейджа "Written by AI" в заголовке |
| **hime** | 6/10 | Нет архитектуры, нет упоминания ИИ, нет секции команда |
| **ino** | 4/10 | Нет архитектуры, конфигурации, деплоя, ИИ, команды |
| **gera** | 0/10 | README.md отсутствует |

**Последствия:** разработчики тратят время на понимание проектов, новички не могут быстро начать работу, команда не знает кто за что отвечает.

---

## Решение

### 1. Обязательные секции README.md

Каждый README **обязан** содержать следующие секции (порядок фиксирован):

| # | Секция | Обязательность | Описание |
|---|--------|---------------|----------|
| 1 | **Заголовок + бейджи** | 🔴 | Название, краткое описание, бейджи (build, version, AI badge) |
| 2 | **Пометка "Written by AI"** | 🔴 | Бейдж + текстовая строка о том что проект создан ИИ |
| 3 | **Архитектура** | 🔴 | Диаграмма (ASCII) или текстовое описание слоёв |
| 4 | **Быстрый старт** | 🔴 | Пошаговая инструкция запуска за < 5 минут |
| 5 | **Конфигурация** | 🔴 | Таблица переменных окружения с описаниями и дефолтами |
| 6 | **API / Эндпоинты** | 🟡 | Если проект предоставляет REST/MCP/gRPC API |
| 7 | **CLI** | 🟡 | Если проект имеет командную строку |
| 8 | **Деплой** | 🔴 | Docker Compose, production-замечания |
| 9 | **Команды разработчика** | 🔴 | test, lint, build, format |
| 10 | **Структура проекта** | 🟡 | Дерево файлов с пояснениями |
| 11 | **Команда Argenta Team** | 🔴 | Таблица агентов проекта |
| 12 | **Лицензия** | 🔴 | MIT |

### 2. Пометка "Written by AI"

**Места размещения:**

1. **В заголовке README** — бейдж:
   ```markdown
   ![Written by AI](https://img.shields.io/badge/Written%20by-AI-ff69b4)
   ```

2. **Под заголовком** — текстовая строка:
   ```markdown
   > 🤖 Разработано с использованием Large Language Models в рамках Argenta Team.
   ```

3. **В package.json / pyproject.toml** (опционально):
   ```json
   "description": "... (Written by AI)"
   ```

**Формулировки (одна из):**
- `Written by AI`
- `Создано ИИ`
- `Разработано с использованием Large Language Models`

### 3. Структура репозитория

Каждый репозиторий **обязан** содержать:

| Файл/Папка | Обязательность | Описание |
|-------------|---------------|----------|
| `README.md` | 🔴 | Документация проекта |
| `LICENSE` | 🔴 | Лицензия (MIT) |
| `.env.example` | 🔴 | Шаблон переменных окружения |
| `.gitignore` | 🔴 | Игнорируемые файлы |
| `.dockerignore` | 🟡 | Если есть Docker |
| `Dockerfile` | 🟡 | Если проект деплоится в Docker |
| `docker-compose.yml` | 🟡 | Если есть зависимости |
| `docs/` | 🟡 | Дополнительная документация |
| `tests/` | 🔴 | Тесты |

**Для Python проектов:**
- `pyproject.toml` или `requirements.txt`
- `migrations/` (если есть БД)

**Для Node.js проектов:**
- `package.json`
- `tsconfig.json` (если TypeScript)
- `src/`

### 4. Стиль README

#### Язык
- **Основной:** русский
- **Для кода и терминов:** английский (MCP, Docker, FastAPI, pgvector)
- Не смешивать языки в предложениях

#### Форматирование
- Заголовки: `#` — название, `##` — секции, `###` — подсекции
- Код: тройные обратные кавычки с указанием языка (`bash`, `python`, `json`)
- Таблицы: для списков параметров, API, переменных окружения
- Разделители: `---` между крупными секциями

#### Эмодзи
- Умеренно, для визуального разделения секций
- Не более 1-2 на секцию
- Примеры: 🚀 Быстрый старт, ⚙️ Конфигурация, 📊 Мониторинг, 🔒 Безопасность

#### Бейджи (верх README)
```markdown
![Python](https://img.shields.io/badge/python-3.12-blue)
![Version](https://img.shields.io/badge/version-1.0.0-green)
![License](https://img.shields.io/badge/license-MIT-yellow)
![Written by AI](https://img.shields.io/badge/Written%20by-AI-ff69b4)
![Argenta Team](https://img.shields.io/badge/Argenta%20Team-purple)
```

### 5. Шаблон README.md

```markdown
# {Название проекта}

{Однострочное описание проекта}

![Python](https://img.shields.io/badge/{lang}-{version}-blue)
![Version](https://img.shields.io/badge/version-{version}-green)
![License](https://img.shields.io/badge/license-MIT-yellow)
![Written by AI](https://img.shields.io/badge/Written%20by-AI-ff69b4)
![Argenta Team](https://img.shields.io/badge/Argenta%20Team-purple)

---

> 🤖 Разработано с использованием Large Language Models в рамках Argenta Team.

---

## Архитектура

```
{ASCII-диаграмма архитектуры}
```

{Описание слоёв}

---

## Быстрый старт

### Предварительные требования

- {Требования}

### Установка

```bash
# 1. Клонировать
git clone {url}
cd {project}

# 2. Настроить
cp .env.example .env

# 3. Запустить
{команда запуска}
```

---

## Конфигурация

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `{VAR}` | {описание} | {дефолт} |

---

## {API / CLI} (если применимо)

{Таблица эндпоинтов или команд}

---

## Деплой

```bash
{Docker Compose или инструкция}
```

---

## Команды разработчика

| Команда | Описание |
|---------|----------|
| `{cmd}` | {описание} |

---

## Структура проекта

```
{дерево файлов}
```

---

## Команда Argenta Team

| Роль | Имя | Специализация |
|------|-----|---------------|
| Team Lead | Афина | Архитектура, координация |
| Programmer | Сона | Код |
| Tech Writer | Тиамат | Документация |

{остальные агенты проекта}

**Разработчик:** Серёжа (Dek1m)

---

## Лицензия

MIT
```

---

## Примеры

### Пример 1: Python проект (selti)

```markdown
# athena-memory — Semantic Memory MCP Server

**athena-memory** — высокопроизводительный MCP-сервер семантической памяти для AI-агентов.

![Python](https://img.shields.io/badge/python-3.12-blue)
![Version](https://img.shields.io/badge/version-0.5.0-green)
![License](https://img.shields.io/badge/license-MIT-yellow)
![Written by AI](https://img.shields.io/badge/Written%20by-AI-ff69b4)
![Argenta Team](https://img.shields.io/badge/Argenta%20Team-purple)

---

> 🤖 Разработано с использованием Large Language Models в рамках Argenta Team.

---

## Архитектура

```
Client (MCP over SSE)
       │
       ▼
   FastAPI / FastMCP ─── Auth Middleware
       │
       ▼
   ┌─────────────────────────────────────────┐
   │            MCP Tools (16)               │
   └──────────────────────┬──────────────────┘
                          │
                          ▼
   ┌─────────────────────────────────────────┐
   │           MemoryService                 │
   └──────┬──────────────────────┬───────────┘
          │                      │
          ▼                      ▼
   ┌───────────┐        ┌───────────────┐
   │ DedupEngine│◄──────►│ Embedding API │
   └─────┬─────┘        └───────┬───────┘
         │                      │
         ▼                      ▼
   PostgreSQL 17 + pgvector (HNSW)
```

**Слои:**

- **MCP Tools** — 16 инструментов для управления памятью
- **DedupEngine** — двухуровневая дедупликация (exact + semantic)
- **MemoryService** — бизнес-логика
- **Repository** — доступ к данным через asyncpg
- **PostgreSQL / pgvector** — векторное хранилище

---

## Быстрый старт

### Предварительные требования

- Docker 24+ и Docker Compose v2
- Python 3.12 (для миграций)

### Установка

```bash
# 1. Клонировать
git clone https://github.com/Dek1m/selti.git
cd selti

# 2. Настроить
cp .env.example .env

# 3. Сгенерировать пароли
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 4. Заполнить .env:
#    PG_PASSWORD, APP_PASSWORD, REDIS_PASSWORD

# 5. Запустить
docker compose --profile local-db up -d

# 6. Проверить
curl http://localhost:8000/health
```

---

## Конфигурация

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql+asyncpg://athena:athena@localhost:5432/athena_memory` |
| `REDIS_URL` | Redis connection string | `redis://:@redis:6379/0` |
| `EMBEDDING_API_URL` | URL API эмбеддингов | `http://10.0.0.21:8080/v1` |
| `API_KEY` | Ключ аутентификации | (пусто) |
| `LOG_LEVEL` | Уровень логирования | `INFO` |

{полная таблица — 30+ переменных}

---

## API

### MCP Tools

| Tool | Описание |
|------|----------|
| `memory_store` | Сохранить запись |
| `memory_search` | Векторный поиск |
| `memory_get` | Получить по ID |
| ... | ... |

### HTTP Endpoints

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/health` | Health check |
| `GET` | `/metrics` | Prometheus метрики |

---

## Деплой

```bash
# Development
docker compose up -d

# Production
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## Команды разработчика

| Команда | Описание |
|---------|----------|
| `pytest tests/ -v` | Запуск тестов |
| `pytest tests/ --cov=memory_server` | Тесты + покрытие |
| `python migrations/run.py` | Применить миграции |
| `celery -A memory_server.celery_app worker -l INFO` | Запуск воркера |

---

## Команда Argenta Team

| Роль | Имя | Специализация |
|------|-----|---------------|
| Team Lead | Афина | Архитектура, координация |
| Architect | Эна | Высокоуровневая архитектура |
| Programmer | Сона | Python, FastAPI |
| Tester | Катерина | Тестирование |
| DB Architect | Нора | PostgreSQL, pgvector |
| DevOps | Рэй | Docker, CI/CD |
| Tech Writer | Тиамат | Документация |
| Memory-Granulator | Тишь | Грануляция знаний |

**Разработчик:** Серёжа (Dek1m)

---

## Лицензия

MIT
```

### Пример 2: Node.js проект (akame)

```markdown
# akame

> opencode-плагин для автоматической грануляции диалогов и кода в семантическую память

![TypeScript](https://img.shields.io/badge/typescript-5.0-blue)
![Node.js](https://img.shields.io/badge/node.js-22-green)
![Version](https://img.shields.io/badge/version-1.0.0-green)
![License](https://img.shields.io/badge/license-MIT-yellow)
![Written by AI](https://img.shields.io/badge/Written%20by-AI-ff69b4)
![Argenta Team](https://img.shields.io/badge/Argenta%20Team-purple)

---

> 🤖 Разработано с использованием Large Language Models в рамках Argenta Team.

---

## Архитектура

```
opencode (Bun/Node.js)
  │
  ├── Session Handler (session.idle, compacted, diff)
  ├── File Handler (file.edited, watcher)
  ├── Tool Handler (tool.execute.after)
  │
  ▼
akame Plugin
  ├── Collector → Granulator Engine
  ├── LLM Agent (memory-granulator / Тишь)
  ├── Tool: granulate_output → athena-memory
  └── Link Enricher (пост-обработка)
  │
  ▼
athena-memory (MCP over HTTP)
  └── PostgreSQL + pgvector
```

**Компоненты:**

- **Event Handlers** — реагируют на события opencode
- **Granulator Engine** — собирает контекст, вызывает LLM
- **Tools** — 7 кастомных инструментов для грануляции
- **Link Enricher** — создаёт связи между гранулами

---

## Быстрый старт

### Установка

```bash
# 1. Скопировать плагин
cp -r akame .opencode/plugins/akame

# 2. Установить зависимости
cd .opencode/plugins/akame
npm install
npm run build

# 3. Настроить .env
cp .env.example .env
```

### Подключение

```json
{
  "plugins": {
    "akame": {
      "source": ".opencode/plugins/akame",
      "enabled": true
    }
  }
}
```

### Проверка

Запустите opencode. В логах должно появиться:
```
akame загружен (userId: akame)
```

---

## Конфигурация

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `AKAME_MCP_URL` | URL athena-memory | `http://athena-memory:8000/mcp/` |
| `AKAME_API_KEY` | API-ключ | — |
| `AKAME_USER_ID` | Владелец записей | `akame` |
| `AKAME_COOLDOWN_MS` | Cooldown между грануляциями | `30000` |

---

## Tools

| Tool | Описание |
|------|----------|
| `granulate_output` | Сохранение гранул в athena-memory |
| `code_index` | Сканирование .ts/.py файлов |
| `code_diff` | Анализ unified diff |
| `code_graph` | Построение графа зависимостей |
| `dependency_analyzer` | Анализ импортов |
| `migrate_legacy_granules` | Миграция старых гранул |
| `graph_health` | Проверка здоровья графа |

---

## Деплой

Плагин не требует отдельного деплоя — он является частью opencode.

---

## Команды разработчика

| Команда | Описание |
|---------|----------|
| `npm run build` | Сборка |
| `npm test` | Запуск тестов |
| `npm run lint` | Проверка стиля |

---

## Структура проекта

```
akame/
├── src/
│   ├── index.ts          # PluginModule + Hooks
│   ├── config.ts         # Конфигурация
│   ├── mcp/client.ts     # HTTP-клиент
│   ├── granulator/       # Ядро грануляции
│   ├── tools/            # 7 кастомных инструментов
│   ├── events/           # Обработчики событий
│   └── security/         # Валидация
├── tests/
├── docs/
└── README.md
```

---

## Команда Argenta Team

| Роль | Имя | Специализация |
|------|-----|---------------|
| Team Lead | Афина | Архитектура, координация |
| Architect | Эна | Высокоуровневая архитектура |
| Programmer | Сона | TypeScript, opencode |
| Tester | Катерина | Тестирование |
| DevOps | Рэй | CI/CD |
| Tech Writer | Тиамат | Документация |
| Memory-Granulator | Тишь | Грануляция знаний |

**Разработчик:** Серёжа (Dek1m)

---

## Лицензия

MIT
```

---

## Checklist для ревью README

- [ ] Бейджи вверху (build, version, AI, Argenta Team)
- [ ] Пометка "Written by AI" — бейдж + текст
- [ ] Архитектура — ASCII-диаграмма или описание
- [ ] Быстрый старт — пошагово, < 5 минут до первого запуска
- [ ] Конфигурация — таблица с описаниями и дефолтами
- [ ] API / CLI — таблица эндпоинтов/команд
- [ ] Деплой — Docker Compose или инструкция
- [ ] Команды разработчика — test, lint, build
- [ ] Структура проекта — дерево файлов
- [ ] Команда — таблица агентов проекта
- [ ] Лицензия — MIT
- [ ] Язык — русский, код — английский
- [ ] Эмодзи — умеренно

---

## Миграция существующих проектов

| Приоритет | Проект | Действия |
|-----------|--------|----------|
| 🔴 P0 | gera | Создать README.md с нуля |
| 🔴 P0 | ino | Добавить: архитектуру, конфигурацию, деплой, команду, ИИ-бейдж |
| 🟡 P1 | hime | Добавить: архитектуру, команду, ИИ-бейдж |
| 🟡 P1 | selti | Добавить: ИИ-бейдж в заголовок |
| 🟢 P2 | akame | Добавить: ИИ-бейдж в заголовок |

---

## Связь с другими решениями

- **AGENTS.md** — общие правила для всех агентов, включая Тиамат
- **ADR-012 (Organic infra gathering)** — инфраструктура проектов
- **Документация akame** — пример хорошей документации (docs/)

---

## Принято

После утверждения Милордом:
1. Создать README.md для gera
2. Обновить ino, hime, selti, akame по чеклисту
3. Добавить ADR-014 в `docs/` каждого проекта
