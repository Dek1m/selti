import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://svc_athene_ai:changeme@localhost:5432/memory")
    db_min_connections: int = 2
    db_max_connections: int = 10

    embedding_api_url: str = "http://10.0.0.21:8080/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen3-embedding-8b"
    embedding_dimension: int = 4096

    # ── Qdrant: векторное хранилище ──
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "memories"
    qdrant_api_key: str = ""  # для Qdrant Cloud (опционально)
    qdrant_enabled: bool = True  # False = только PostgreSQL без векторного поиска

    mcp_server_name: str = os.getenv("SERVICE_NAME", "selti")
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    search_default_limit: int = 10
    search_default_threshold: float = 0.7

    # ── Фаза 1: hybrid search + ранжирование (D4/D5) ──
    # Фича-флаг отката (не legacy): False = плотный Qdrant-путь Фазы 0.
    hybrid_search_enabled: bool = True
    hybrid_prefetch: int = 100   # кандидатов на канал до RRF-fusion
    rrf_k: int = 60              # RRF: score = Σ 1/(k + rank)
    mmr_lambda: float = 0.7      # MMR: баланс relevance/diversity

    # Ранжирование D4: score = rrf × recency_decay × importance_weight.
    # decay = rate^(дней с COALESCE(last_accessed_at, created_at)); frozen → 1.0.
    recency_decay_rate: float = 0.995  # затухание в день (неймспейс без override)
    recency_decay_rates: dict[str, float] = {
        "default": 0.995,
        "user_facts": 0.999,        # факты о пользователе долговечны
        "project_meta": 0.998,      # архитектурные решения живут долго
        "code_knowledge": 0.995,
        "dialogue_insights": 0.99,  # инсайты разговоров устаревают быстрее
        "infrastructure": 0.993,    # инфра-топология меняется заметно
    }
    importance_multipliers: dict[str, float] = {
        "default": 1.0,
        "user_facts": 1.2,          # факты о пользователе — приоритет
        "project_meta": 1.1,
        "code_knowledge": 1.0,
        "dialogue_insights": 0.8,   # разговорный контент мягже кода
        "infrastructure": 1.0,
    }

    traverse_max_nodes: int = 500  # cap узлов обхода графа (Фаза 1.5)

    # ── Фаза 2: жизненный цикл гранул (D3/D4) ──
    # Supersession: уверенность наследуется ×0.9 (cap 0..1) — каждое
    # перепрохождение факта через систему стоит части уверенности.
    supersession_confidence_factor: float = 0.9
    # Physical decay: confidence *= recency_decay_rates[namespace] ежедневно;
    # ниже floor гранула не затухает дальше (кандидат в mark_stale/GC-ревизию).
    confidence_decay_floor: float = 0.1
    stale_threshold: float = 0.3  # confidence ниже порога + нет доступа N дней
    stale_days: int = 30          # «нет доступа» = COALESCE(last_accessed_at, created_at) старше
    # GC superseded-версий: hard delete только с наследником и старше retention.
    gc_retention_days: int = 90
    gc_dry_run: bool = True       # dry-run первый месяц (Рэй переключит на проде)
    # Кластеризация Level 2 (022 v2): кандидаты — Qdrant ANN (HNSW, cosine).
    # cluster_threshold — порог score в Qdrant (близость эмбеддингов, не
    # триграммы v1); top_k соседей на гранулу; группы < min_members
    # кластером не считаются (singleton-вершины отсеивает хранимка).
    cluster_threshold: float = 0.92
    cluster_top_k: int = 10
    cluster_min_members: int = 2

    dedup_enabled: bool = True
    dedup_threshold: float = 0.95
    dedup_thresholds: dict[str, float] = {
        "default": 0.95,
        "user_facts": 0.90,
        "dialogue_insights": 0.85,
        "code_knowledge": 0.95,
        "project_meta": 0.90,
        "infrastructure": 0.95,
    }

    # ── Фаза 6: «облачко знаний» (D9) ──
    # TTL Redis-кеша ctx:{slug} и dirty-флага (синхронизирован с периодом
    # beat rebuild_contexts: флаг живёт не дольше периода пересборки).
    context_cache_ttl: int = 3600
    # Период полураспада важности при отборе кандидатов облачка: за 30 дней
    # гранула теряет половину веса — свежие решения всплывают над древними.
    cloud_recency_half_life_days: int = 30

    api_key: str = ""

    # ── Фаза 5: веб-морда ──
    # CORS под фронт-порт (Vite default 5173); переопределяется env-JSON
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # ── Регистрация проектов (ADR-018) ──
    # Непустой → POST /projects/register требует заголовок X-SELTI-KEY;
    # пустой → эндпоинт открыт (совместимость с существующими клиентами).
    selti_api_key: str = ""

    redis_url: str = "redis://:@redis:6379/0"

    # ── Celery: асинхронные задачи ──
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"
    celery_task_serializer: str = "json"
    celery_result_serializer: str = "json"
    celery_accept_content: list[str] = ["json"]
    celery_timezone: str = "UTC"
    celery_worker_concurrency: int = 4
    celery_worker_prefetch_multiplier: int = 1
    celery_worker_max_tasks_per_child: int = 1000
    celery_worker_max_memory_per_child: int = 200000  # 200MB

    log_level: str = "INFO"

    uvicorn_workers: int = 1

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
