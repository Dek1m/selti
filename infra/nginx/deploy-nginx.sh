#!/bin/bash
# deploy-nginx.sh — деплой nginx reverse proxy на ai.atom.ui
# Запускать из директории /home/svc_athene_ai@atom.ui/app/

set -euo pipefail

COMPOSE_DIR="/home/svc_athene_ai@atom.ui/app"
CERTS_DIR="${COMPOSE_DIR}/certs"
NGINX_DIR="${COMPOSE_DIR}/nginx"

echo "=== Деплой nginx reverse proxy ==="

# 1. Проверяем что сертификаты на месте
if [ ! -f "${CERTS_DIR}/fullchain.pem" ] || [ ! -f "${CERTS_DIR}/privkey.pem" ]; then
    echo "ОШИБКА: Сертификаты не найдены в ${CERTS_DIR}/"
    echo "Нужны: fullchain.pem и privkey.pem"
    echo ""
    echo "Скопируйте сертификаты:"
    echo "  mkdir -p ${CERTS_DIR}"
    echo "  cp /путь/к/fullchain.pem ${CERTS_DIR}/"
    echo "  cp /путь/к/privkey.pem ${CERTS_DIR}/"
    echo "  chmod 600 ${CERTS_DIR}/privkey.pem"
    exit 1
fi

echo "[1/4] Сертификаты найдены ✓"

# 2. Проверяем что контейнеры в нужной сети
echo "[2/4] Проверка сети..."
docker network inspect app_default >/dev/null 2>&1 || {
    echo "Создаю сеть app_default..."
    docker network create app_default
}
echo "Сеть app_default существует ✓"

# 3. Собираем и запускаем nginx
echo "[3/4] Сборка и запуск nginx..."
cd "${COMPOSE_DIR}"
docker compose up -d --build nginx

# 4. Проверяем health
echo "[4/4] Проверка health..."
sleep 5
if docker exec nginx-proxy wget -qO- http://localhost/health 2>/dev/null | grep -q "ok"; then
    echo "nginx healthy ✓"
else
    echo "ОШИБКА: nginx не отвечает. Проверь логи: docker logs nginx-proxy"
    exit 1
fi

echo ""
echo "=== Готово ==="
echo "opencode:  https://opencode.atom.ui"
echo "selti:     https://memory.atom.ui"
echo ""
echo "Проверка:"
echo "  curl -I https://opencode.atom.ui"
echo "  curl -I https://memory.atom.ui"
echo "  docker logs nginx-proxy -f"
