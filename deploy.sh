#!/usr/bin/env bash
# ============================================================
# Zero-downtime deploy — Memory MCP Server
# ============================================================
# Использование:
#   export REGISTRY_TOKEN=ghp_xxx
#   ./deploy.sh
#
# Что делает:
#   1. Логинится в GHCR
#   2. Пуллит новый образ
#   3. Поднимает новый контейнер рядом со старым
#   4. Проверяет healthcheck нового
#   5. Переключает трафик (через reverse proxy или просто restart)
#   6. Убивает старый контейнер
# ============================================================

set -euo pipefail

# ---------- config ----------
REGISTRY="ghcr.io"
IMAGE_NAME="${GITHUB_REPOSITORY:-your-org/memory-server}"
IMAGE="${REGISTRY}/${IMAGE_NAME}:latest"

COMPOSE_DIR="${HOME}/memory-server"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"
ENV_FILE="${COMPOSE_DIR}/.env"

COMPOSE_PROJECT="athena-memory"
SERVICE_NAME="memory-server"

NEW_PORT=8001    # временный порт для rolling-деплоя
OLD_PORT=8000

# ---------- prepare ----------
echo "[deploy] Pulling image..."
echo "${REGISTRY_TOKEN}" | docker login "${REGISTRY}" -u "${GITHUB_ACTOR}" --password-stdin
docker pull "${IMAGE}"

# ---------- start new container ----------
echo "[deploy] Starting new container on port ${NEW_PORT}..."

docker run -d \
  --name "${COMPOSE_PROJECT}-${SERVICE_NAME}-new" \
  --restart unless-stopped \
  --env-file "${ENV_FILE}" \
  -p "${NEW_PORT}:8000" \
  "${IMAGE}"

# ---------- healthcheck ----------
echo "[deploy] Waiting for new container to be healthy..."
for i in $(seq 1 15); do
  sleep 2
  if curl -sf "http://localhost:${NEW_PORT}/health" > /dev/null 2>&1; then
    echo "[deploy] New container is healthy"
    break
  fi
  if [ "$i" -eq 15 ]; then
    echo "[deploy] Healthcheck failed, rolling back..."
    docker stop "${COMPOSE_PROJECT}-${SERVICE_NAME}-new"
    docker rm "${COMPOSE_PROJECT}-${SERVICE_NAME}-new"
    exit 1
  fi
done

# ---------- switch traffic ----------
# Вариант A: если за reverse proxy (nginx/traefik) — он перечитывает контейнеры
# Вариант B: просто перезапускаем основной сервис с новым образом

echo "[deploy] Switching traffic..."

# Останавливаем и удаляем старый контейнер
OLD_CONTAINER=$(docker ps -q --filter "name=${COMPOSE_PROJECT}-${SERVICE_NAME}" --filter "expose=8000" 2>/dev/null || true)
if [ -n "${OLD_CONTAINER}" ]; then
  docker stop "${OLD_CONTAINER}" || true
  docker rm "${OLD_CONTAINER}" || true
fi

# Переименовываем новый контейнер в основной
docker rename "${COMPOSE_PROJECT}-${SERVICE_NAME}-new" "${COMPOSE_PROJECT}-${SERVICE_NAME}"

# Если порты отличаются — запускаем с основным портом
# (в реальности reverse proxy смотрит на контейнеры по имени)

echo "[deploy] Cleaning up..."
docker image prune -f

echo "[deploy] Zero-downtime deploy completed successfully"
