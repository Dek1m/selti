# ============================================================
# Stage 1: build — устанавливаем зависимости
# ============================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Ставим системные зависимости для asyncpg (libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --user -r requirements.txt

# ============================================================
# Stage 2: runtime — минимальный образ + opencode-зависимости
# ============================================================
FROM python:3.12-slim AS runtime

WORKDIR /app

# Системные зависимости + Node.js 22 LTS (через nodesource с GPG) + ssh-клиент
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    openssh-client \
    curl \
    ca-certificates && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y curl && \
    rm -rf /var/lib/apt/lists/* /root/.npm

# Копируем установленные пакеты из builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Копируем код приложения
COPY memory_server/ ./memory_server/
COPY migrations/ ./migrations/

EXPOSE 8000

ENTRYPOINT ["uvicorn", "memory_server.__main__:app", "--host", "0.0.0.0", "--port", "8000"]
