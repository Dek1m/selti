# conf.d nginx прода ai.atom.ui — эталонные копии

Фактический источник правды на сервере: `/etc/nginx/conf.d/` (bind-mount в
контейнер `nginx` целиком: `/etc/nginx/:/etc/nginx/`). Этот каталог —
VCS-копия, чтобы конфигурация прода не жила только на сервере.

## Файлы

| Файл | Назначение |
|---|---|
| `ai-landing.conf` | ai.atom.ui: лендинг + `/ui/` (морда, LAN-only) + `/api/` → selti |
| `memory.conf` | memory.atom.ui → selti:8000 (API/MCP) |
| `login.conf` | login.atom.ui / keycloak.atom.ui → 10.0.0.23:8080 |
| `zz-selti-api-auth.conf.example` | шаблон LAN-инъекции Bearer (секрет только на сервере!) |

`zz-selti-api-auth.conf` с реальным ключом в VCS НЕ коммитится.

## Порядок синка (репо → сервер)

```bash
# с машины разработчика (механика SSH — tools/diagnostics/RUNBOOK_PROD.md, п. 1)
cat infra/nginx/conf.d/<файл>.conf | ssh svc_athene_ai@ai.atom.ui \
  'sudo -n tee /etc/nginx/conf.d/<файл>.conf >/dev/null'
ssh svc_athene_ai@ai.atom.ui \
  'sudo -n cp /etc/nginx/conf.d/<файл>.conf /etc/nginx/conf.d/<файл>.conf.bak.$(date +%Y%m%d-%H%M%S)  # бэкап ДО замены!'
ssh svc_athene_ai@ai.atom.ui 'docker exec nginx nginx -t && docker exec nginx nginx -s reload'
```

Бэкап оригинала на сервере — обязательно ДО замены.

## Важное (инцидент 2026-09-22, P1 «вебморда не коннектится»)

- **Никаких статических `upstream { server selti:8000; }`** для контейнерных
  upstream-ов: nginx резолвит имя один раз при старте, деплой пересоздаёт
  контейнер, IP меняется → 502 до ручного reload. Только runtime-резолв:
  `resolver 127.0.0.11 valid=10s ipv6=off;` + `set $selti_upstream
  "http://selti:8000"; proxy_pass $selti_upstream;`
- **Healthcheck'и бьют в `/live`** (liveness, мгновенный), НЕ в `/health`
  (readiness: PG+Redis+celery inspect, стабильно ~5 c — docker healthcheck
  с timeout 3 c всегда падал, отсюда миф о «ложном unhealthy»).
- Серверный compose (`~/app/docker-compose.yml`) тоже правится на сервере
  (history в `*.bak*` рядом). Healthcheck nginx там двухплечевой:
  `http://127.0.0.1/nginx-health` (сам nginx, 80) И `http://selti:8000/live`
  (плечо до selti).
