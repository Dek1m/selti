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
# Stage 2: runtime — минимальный образ
# ============================================================
FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 && \
    rm -rf /var/lib/apt/lists/*

# Копируем установленные пакеты из builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Копируем код приложения
COPY memory_server/ ./memory_server/
COPY migrations/ ./migrations/

EXPOSE 8000

ENTRYPOINT ["uvicorn", "memory_server.__main__:app", "--host", "0.0.0.0", "--port", "8000"]
