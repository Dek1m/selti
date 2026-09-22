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
    # GC superseded-версий (V3.1, F ADR-019 — стоп-кран полной истории):
    #   gc_purge_enabled — мастер-кран ВЫШЕ режимов: False = физическое
    #     удаление невозможно в принципе (полная история сохраняется всегда);
    #   gc_mode — 'disabled' (дефолт: candidates-only, ничего не удаляем) |
    #     'hard' (hard delete superseded с наследником старше retention;
    #     работает только при gc_purge_enabled=True). 'soft' — зарезервирован
    #     будущими фазами.
    # Заменяет gc_dry_run: прод-эффект дефолтов тот же (ничего не удаляем),
    # мина FK (дыра 7) обезврежена явно, а не сухим прогоном.
    gc_purge_enabled: bool = False
    gc_mode: str = "disabled"
    gc_retention_days: int = 90
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

    # ── Линкер V3 (ADR-019 C, фазы V3.2/V3.3) ──
    # Зоны cosine НЕ пересекаются с dedup: верхняя граница линкера для
    # namespace = dedup_thresholds[ns] (default 0.95), ниже — фиксированные
    # слои: [linker_synonym_threshold, linker_verdict_threshold) — L1a auto
    # related_to; [linker_verdict_threshold, dedup) — L2 LLM-вердикт;
    # < linker_synonym_threshold — тишина (HippoRAG 2: ниже 0.8 шум).
    linker_enabled: bool = True        # мастер-выключатель автолинкинга новых гранул
    linker_l1a_enabled: bool = True    # L1 synonym-слой (риск шума ADR-019.1 — флаг без миграции)
    linker_l1c_enabled: bool = True    # L1 co-occurrence
    # Manual mode L2 (приказ Мастера 20.09): без продового LLM-ключа очередь
    # серой зоны КОПИТСЯ, а разбирает её человек-агент (Тишь) тулами
    # memory_linker_review / memory_linker_verdict. True + пустой
    # linker_llm_base_url = l2_mode "manual"; beat-воркер l2_verdicts в
    # manual-режиме очередь не трогает.
    linker_l2_manual: bool = True
    linker_synonym_threshold: float = 0.80
    linker_verdict_threshold: float = 0.85
    linker_ann_limit: int = 10         # соседей из ANN на новую гранулу (верхний кап L1a)
    linker_top_k: int = 5              # кандидатов в одном L2-промпте
    linker_cooccurrence_cap: int = 10  # max co-occurrence рёбер на гранулу (свежие соседи)
    linker_reconciler_batch: int = 500 # батч name_reconciler (ADR: 500)
    linker_reconciler_dry_run: bool = True  # первая кампания — только отчёт; бой после ручного прогона
    linker_l2_batch: int = 20          # элементов L2-очереди за прогон воркера
    linker_l2_max_attempts: int = 3    # попыток LLM-вердикта на элемент очереди
    # LLM-провайдер L2: пустой base_url = L2 отключён (WARN при старте,
    # L1 работает; очередь не наполняется — сирот подберёт V3.4 orphan_linker).
    linker_llm_base_url: str = ""
    linker_llm_api_key: str = ""
    linker_llm_model: str = "glm-4.7-flash"
    linker_llm_timeout: float = 10.0
    linker_llm_retries: int = 1
    linker_verdict_cache_ttl: int = 30 * 24 * 3600  # 30 дней (ADR-019 C)

    # ── Фаза 3 (волна 3): L1c-гейт + чистка истории co-occurrence ──
    # Одна сессия ≠ смысловая близость: ребро L1c создаётся/выживает только
    # при косинусе эмбеддингов пары ≥ порога (батч retrieve source+соседи,
    # оценка в Python). 0.0 = гейт выключен (все пары проходят). Qdrant
    # недоступен → fail-closed: рёбер нет, гранула вернётся на ретрай.
    linker_l1c_gate_min: float = 0.30
    # One-off кампания prune_cooccurrence_history: пар исторических l1c
    # за итерацию (векторы концов — retrieve-батчами Qdrant по 256).
    linker_l1c_prune_batch: int = 256

    # ── V3.5 «Жизнь графа знаний»: жизнь рёбер (Ф1) + PPR-traverse (Ф2) ──
    # Формулы — вердикты Эны 22.09 (закрывают дыры Д1/Д2 тест-плана):
    # вес ребра НЕ материализуется ежедневным батчем — ЛЕНИВАЯ проекция
    #   w_eff(r,t) = CASE WHEN immune(r) THEN r.weight
    #                      ELSE r.weight * exp(-λ_eff × days(t − COALESCE(
    #                           last_used_at, created_at))) END;
    #   λ_eff = GREATEST(λ_min, λ / (1 + used_count)) — сатурация частоты.
    # Материализует состояние ТОЛЬКО pruning-кампания (pruned_at, не DELETE).
    # Мастер-выключатель Ф1: False (дефолт) = reinforce-хуки и prune-кампания
    # молчат — бой включается осознанно после стенд-репетиции.
    edge_lifecycle_enabled: bool = False
    edge_reinforcement_enabled: bool = True
    edge_decay_lambda: float = 0.02       # λ: затухание в день (1/день)
    edge_decay_lambda_min: float = 0.002  # λ_min: насыщение частых рёбер
    edge_decay_floor: float = 0.05        # порог отсечения: raw w_eff ≤ floor
    edge_prune_min_age_days: int = 30     # кандидат: возраст created_at
    edge_prune_dry_run: bool = True       # первая кампания — только отчёт
    edge_reinforce_alpha: float = 0.2     # reinforce: w += (1−w)×α (cap 1.0)

    # Ф2: traverse(strategy="activation") — PPR на CSR. Directed-переходы
    # (эталон 5.1 тест-плана); damping 0.85, power iteration 25, топ-K.
    # False до приёмки: strategy=activation → внятная ошибка, не тихий bfs.
    traverse_activation_enabled: bool = False
    ppr_damping: float = 0.85
    # 25 итераций (вердикт Эны 23.09): остаточная осцилляция циклов
    # d^n: 0.85^15≈0.087 переворачивает топ-3 на циклах, 0.85^25≈0.017
    # даёт эталонную точность ±0.02; цена — ~+1.1 мс на 106k рёбер.
    traverse_activation_iterations: int = 25
    traverse_activation_top_k: int = 50
    # Зеркала симметричных типов в activation-графе (вердикт Эны 23.09):
    # link_type из списка получает встречную дугу target→source с тем же
    # w_eff (related_to не имеет стрелки). Направленные типы (depends_on,
    # contradicts, supersedes, solves, references, …) — строго directed.
    traverse_symmetric_link_types: list[str] = ["related_to"]
    # Порог потока reinforce при activation (вердикт Эны 23.09): ребро-
    # проводник касаем, только если flow(src→tgt) = r[src]×M[tgt,src] ≥
    # порога и оба конца в топ-K выдачи (одно касание на пару за запрос).
    edge_reinforce_flow_min: float = 0.001

    # ── Ассоциативное расширение search (Фаза 3, требование Мастера:
    # через СТАРЫЙ тул memory_search) ──
    # strategy="activation": фаза 1 — RRF-поиск даёт seed-гранулы (топ
    # search_activation_seed_limit, 8–12), фаза 2 — PPR-распространение
    # ActivationSpreader'ом по живому графу, фаза 3 — seed + активированные
    # соседи (поле activated=true), общий размер = limit поиска.
    # False до приёмки: activation → внятная ошибка, гибрид бит-в-бит прежний.
    search_activation_enabled: bool = False
    search_activation_seed_limit: int = 10

    api_key: str = ""

    # ── Полная карта 3D (PLAN_FULL_MAP_3D, M1/M2) ──
    # Серверная раскладка: igraph DrL dim=3 → нормировка в куб →
    # min-distance-релаксация. Координаты целые в [-bbox, bbox]³.
    map_layout_bbox: int = 1000        # полу-сторона куба нормировки
    map_min_dist: float = 50.0         # мин. дистанция расталкивания пар (единицы bbox)
    map_relax_iterations: int = 8      # итераций релаксации (ранний выход при стабилизации)
    map_drl_timeout: float = 120.0     # лимит spawn-субпроцесса DrL (сегфолт-щит, фикс F1)
    # Снапшот: кеш /full (gz-байты) и /meta в Redis
    map_meta_ttl: int = 60             # кеш меты, с (план: <50мс на запрос)
    map_snapshot_ttl: int = 86400      # TTL снапшота текущей версии, с
    map_stale_ttl: int = 300           # EXPIRE устаревших версий снапшота, с
    map_build_wait_seconds: float = 60.0  # ожидание конкурента под build-lock, с
    # Усечение полей узла при сборке снапшота
    map_preview_chars: int = 180       # preview контента, по границе слова + «…»
    map_name_chars: int = 80           # entity_name (тултип)

    # ── Galactic Layout v2 (GALACTIC_LAYOUT.md) ──
    # Защитный порог масштаба (прод-OOM 20.09: пик >3 ГБ при лимите 512M):
    # выше порога таска пропускает раскладку с WARNING — карта остаётся на
    # сфере. Поднять/снять после подтверждения прод-замеров Рэем.
    galactic_max_nodes: int = 20_000
    galactic_max_edges: int = 150_000
    galactic_max_clusters: int = 3_000

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
