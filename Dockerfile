# ============================================================
# Stage 1: build — устанавливаем зависимости
# ============================================================
FROM python:3.14-slim AS builder

WORKDIR /build

# Ставим системные зависимости для asyncpg (libpq)
RUN apt-get update && apt-get install -y --no-install-recommends     git gcc libpq-dev &&     rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --user -r requirements.txt

# ============================================================
# Stage 2: runtime — минимальный образ
# ============================================================
FROM python:3.14-slim AS runtime

WORKDIR /app

# Системные зависимости + Node.js 22 LTS + ssh-клиент
RUN apt-get update && apt-get install -y --no-install-recommends     libpq5     openssh-client     curl     ca-certificates     gnupg &&     curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key         | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg &&     echo deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main         > /etc/apt/sources.list.d/nodesource.list &&     apt-get update && apt-get install -y --no-install-recommends nodejs &&     apt-get purge -y curl &&     rm -rf /var/lib/apt/lists/* /root/.npm

# Копируем установленные пакеты из builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Копируем код приложения
COPY memory_server/ ./memory_server/
COPY migrations/ ./migrations/
COPY VERSION ./VERSION

EXPOSE 8000

# --workers 4 = 4 процессов, каждый с event loop
# --loop uvloop = быстрый event loop (uvloop входит в uvicorn[standard])
# --timeout-graceful-shutdown 30 = 30s на graceful shutdown
# --backlog 2048 = очередь соединений
ENTRYPOINT ["uvicorn", "memory_server.__main__:app", \
    "--host", "0.0.0.0", "--port", "8000", \
    "--workers", "4", \
    "--loop", "uvloop", \
    "--timeout-graceful-shutdown", "30", \
    "--backlog", "2048"]
