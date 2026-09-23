"""SettingsRepository — runtime-конфигурация в PostgreSQL (миграция 027).

Реестр ключей — точная проекция docs/SETTINGS_REGISTRY.md (§2, 97 ключей):
схема валидации (тип, min/max, enum, json-подсхемы), флаги dangerous /
requires_restart, группы и виджеты UI. Дефолты берутся из config.Settings
(§6.4 реестра: не рассинхронизировать config.py и сидинг 027), для
schedule.* и api_caps — явно здесь (их нет среди полей Settings).

Слои резолва (§1 реестра): env/compose (явно заданный) > БД app_settings >
дефолт config.py. Env-детекция — pydantic model_fields_set: точно отличает
«задано в env» от «совпало с дефолтом».
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping

import asyncpg

from memory_server.config import Settings, settings
from memory_server.logger import get_logger

logger = get_logger(__name__)

# ════════════════════════ Исключения ════════════════════════


class SettingsValidationError(ValueError):
    """Значение не прошло валидацию схемы (HTTP 400)."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or [message]


class SettingsConfirmationError(Exception):
    """Опасная операция без confirm=true (HTTP 409)."""

    def __init__(self, message: str, keys: list[str] | None = None) -> None:
        super().__init__(message)
        self.keys = keys or []


class SettingsLockedError(Exception):
    """Ключ жёстко заблокирован env/compose (HTTP 409)."""


class ProfileError(Exception):
    """Конфликт профиля: имя занято / builtin защищён (409)."""


class ProfileNotFoundError(Exception):
    """Профиль не найден (404)."""


# ════════════════════════ Реестр ключей ════════════════════════

# Симметричные LinkType: PPR-зеркалирование допустимо только для них
# (related_to/alternative_to/connected_to не имеют стрелки; направленные
# типы — строго directed, вердикт Эны 23.09).
SYMMETRIC_LINK_TYPES = frozenset({"related_to", "alternative_to", "connected_to"})

# dict-ключи: namespace → float с обязательным "default"
_NAMESPACE_DICT_SPECS: dict[str, tuple[float, float]] = {
    "recency_decay_rates": (0.9, 1.0),
    "importance_multipliers": (0.1, 3.0),
    "dedup_thresholds": (0.5, 1.0),
}


@dataclass(frozen=True)
class SettingSpec:
    """Схема одного runtime-ключа (проекция строки реестра §2)."""

    key: str
    value_type: str  # 'int' | 'float' | 'bool' | 'str' | 'json'
    group: str
    widget: str = "number"
    dangerous: bool = False
    requires_restart: bool = False
    min_value: float | None = None
    max_value: float | None = None
    enum_values: tuple[str, ...] | None = None
    # Дефолт для ключей без поля в Settings (schedule.*, виджет-схема json)
    default: Any = field(default=None, repr=False)
    # Русские подписи UI — дословно сидинг 027 (последняя инстанция);
    # попадают в upsert, чтобы reset→PUT пересоздавал строку ЦЕЛИКОМ
    title_ru: str = field(default="", repr=False)
    description_ru: str = field(default="", repr=False)


GROUPS: dict[str, str] = {
    "search": "Поиск и ранжирование",
    "dedup": "Дедупликация",
    "lifecycle": "Жизненный цикл и GC",
    "cluster": "Кластеризация",
    "linker": "Линкер",
    "edge": "Рёбра графа",
    "cloud": "Облачко знаний",
    "map": "Карта",
    "celery": "Планировщик: воркер и лимиты задач",
    "schedule": "Планировщик: расписания beat",
    "api_caps": "Лимиты API",
}


# Русские подписи ключей (title/description) — дословно из сидинга 027
# (последняя инстанция; трёхстороннюю сверку гарантирует
# tests/test_settings_registry_sync.py). Порядок — порядок 027.
_RU_TEXTS: dict[str, tuple[str, str]] = {
    # ── §2.1 Поиск и ранжирование ──
    'search_default_threshold': (
        'Порог релевантности поиска',
        'Минимальный балл схожести, ниже которого гранулы не попадают в выдачу, если клиент не задал свой порог.',
    ),
    'hybrid_search_enabled': (
        'Гибридный поиск',
        'Включает гибридный поиск Фазы 1 (плотный вектор + полнотекстовый канал со слиянием RRF). Выключение откатывает на чистый векторный путь Qdrant — фича-флаг отката.',
    ),
    'hybrid_prefetch': (
        'Предвыборка кандидатов',
        'Сколько кандидатов набирается в каждом канале до слияния RRF. Больше — точнее ранжирование, дороже запрос.',
    ),
    'rrf_k': (
        'Коэффициент RRF',
        'Константа сглаживания формулы слияния рангов: score = Σ 1/(k + rank). Меньше k — сильнее вес верхних позиций.',
    ),
    'mmr_lambda': (
        'Баланс MMR',
        'Баланс релевантности и разнообразия выдачи при MMR-переранжировании: 1.0 — чистая релевантность, ниже — больше разнообразия.',
    ),
    'recency_decay_rate': (
        'Затухание свежести (дефолт)',
        'Ежедневный множитель веса гранулы в ранжировании для неймспейсов без своего override. 0.995 ≈ −0.5% в день.',
    ),
    'recency_decay_rates': (
        'Затухание по неймспейсам',
        'Индивидуальные скорости затухания свежести на неймспейс. Факты о пользователе живут дольше (0.999), инсайты разговоров устаревают быстрее (0.99).',
    ),
    'importance_multipliers': (
        'Приоритет неймспейсов',
        'Множитель важности гранулы в ранжировании по её неймспейсу: факты пользователя (1.2) всплывают выше разговорного контента (0.8).',
    ),
    'search_activation_enabled': (
        'Ассоциативный поиск',
        'Включает стратегию `activation` тула memory_search: seed-гранулы расширяются соседями по графу связей (Personalized PageRank). До включения стратегия возвращает внятную ошибку.',
    ),
    'search_activation_seed_limit': (
        'Лимит seed-гранул',
        'Сколько лучших прямых попаданий берётся как затравка для ассоциативного расширения.',
    ),
    # ── §2.2 Дедупликация ──
    'dedup_enabled': (
        'Дедупликация записей',
        'При записи гранула сравнивается с существующими по смысловой близости; дубль не создаётся. Выключение допускает дубли — включать осознанно.',
    ),
    'dedup_threshold': (
        'Порог дедупликации',
        'Косинусная близость, выше которой новая гранула считается дублем существующей.',
    ),
    'dedup_thresholds': (
        'Пороги по неймспейсам',
        'Индивидуальные пороги дедупликации: для разговорных инсайтов планка ниже (0.85 — формулировки варьируются сильнее), для кода — выше.',
    ),
    # ── §2.3 Жизненный цикл и GC ──
    'supersession_confidence_factor': (
        'Наследование уверенности',
        'При создании новой версии гранулы уверенность наследуется с этим множителем (cap 0..1): каждое перепрохождение факта через систему стоит части уверенности.',
    ),
    'confidence_decay_floor': (
        'Пол затухания уверенности',
        'Ниже этого уровня ежедневное затухание останавливается: гранула не выродится в ноль, а станет кандидатом на ревизию (mark_stale).',
    ),
    'stale_threshold': (
        'Порог устаревания',
        'Уверенность ниже порога + нет доступа `stale_days` дней → гранула помечается устаревшей и попадает в очередь ревизии.',
    ),
    'stale_days': (
        'Дней без доступа',
        'Сколько дней гранула должна не запрашиваться, чтобы считаться заброшенной при упавшей уверенности.',
    ),
    'gc_purge_enabled': (
        'Мастер-кран физического удаления',
        'False — физическое удаление знаний невозможно в принципе (полная история сохраняется всегда). True ОТКРЫВАЕТ hard delete устаревших версий. Включать только осознанно после бэкапа.',
    ),
    'gc_mode': (
        'Режим GC',
        'disabled — ничего не удаляется (только отчёт кандидатов); hard — физическое удаление superseded-версий старше retention (работает только при включённом мастер-кране); soft — зарезервирован будущими фазами.',
    ),
    'gc_retention_days': (
        'Срок хранения версий',
        'Сколько дней после замены версии GC в режиме hard держит superseded-копию перед физическим удалением.',
    ),
    # ── §2.4 Кластеризация ──
    'cluster_threshold': (
        'Порог близости кластеров',
        'Минимальная близость эмбеддингов (score в Qdrant) для попадания соседа в кандидаты кластера при ночной разметке.',
    ),
    'cluster_top_k': (
        'Соседей на гранулу',
        'Сколько ближайших соседей рассматривается для каждой гранулы при сборке кластеров. Больше — крупнее кластеры, дольше расчёт.',
    ),
    'cluster_min_members': (
        'Минимум участников',
        'Группы меньше этого размера кластером не считаются (остаются одиночными вершинами).',
    ),
    # ── §2.5 Линкер ──
    'linker_enabled': (
        'Автолинкинг',
        'Мастер-выключатель автолинкинга новых гранул. Выключение останавливает построение новых связей знаний — включать осознанно.',
    ),
    'linker_l1a_enabled': (
        'Слой L1a (синонимы)',
        'Автосвязи «related_to» по ANN-поиску синонимов эмбеддингов. Выключается при риске шума связей (флаг отката ADR-019.1).',
    ),
    'linker_l1c_enabled': (
        'Слой L1c (совместные упоминания)',
        'Связи между гранулами, встречавшимися в одном контексте (co-occurrence).',
    ),
    'linker_l2_manual': (
        'Ручной режим L2',
        'True — «серую зону» близости разбирает человек-агент тулами memory_linker_review/verdict; False — очередь отдаётся LLM-воркеру. Режим назначен приказом Мастера 20.09.',
    ),
    'linker_synonym_threshold': (
        'Порог синонимии L1a',
        'Нижняя граница «серой зоны»: ниже — тишина (шум), выше начинается auto-related_to. Должен быть ниже вердиктного порога.',
    ),
    'linker_verdict_threshold': (
        'Порог LLM-вердикта',
        'Верхняя граница auto-слоя: от этого порога до порога дедупликации пару связывает только явный вердикт (LLM или человек).',
    ),
    'linker_ann_limit': (
        'Соседей ANN на гранулу',
        'Верхний кап кандидатов синонимии из векторного поиска на одну новую гранулу.',
    ),
    'linker_top_k': (
        'Кандидатов в L2-промпте',
        'Сколько пар-кандидатов попадает в один LLM-запрос вердикта.',
    ),
    'linker_cooccurrence_cap': (
        'Кап co-occurrence рёбер',
        'Максимум L1c-рёбер на гранулу (приоритет свежим соседям) — защита от разрастания графа.',
    ),
    'linker_reconciler_batch': (
        'Батч резолва имён',
        'Сколько «висячих» ссылок name_reconciler обрабатывает за итерацию кампании.',
    ),
    'linker_reconciler_dry_run': (
        'Резолв имён: сухой режим',
        'True — кампания только строит отчёт, ничего не переписывает. False — боевой резолв ссылок. Переключать после ручной проверки первого отчёта.',
    ),
    'linker_l2_batch': (
        'Размер L2-батча',
        'Сколько элементов серой зоны обрабатывается за прогон воркера/агента.',
    ),
    'linker_l2_max_attempts': (
        'Попыток L2-вердикта',
        'Сколько раз сбойный элемент очереди возвращается в обработку, прежде чем отбрасывается с WARNING.',
    ),
    'linker_l1c_gate_min': (
        'Гейт L1c по косинусу',
        'Ребро co-occurrence живёт только при косинусной близости пары ≥ порога (одна сессия ≠ смысловая близость). 0.0 — гейт выключен.',
    ),
    'linker_l1c_prune_batch': (
        'Батч чистки L1c-истории',
        'Размер пакета исторических пар при one-off кампании перепроверки co-occurrence гейтом.',
    ),
    'linker_verdict_cache_ttl': (
        'Кеш вердиктов (сек)',
        'Сколько секунд хранится вердикт по паре (30 дней) — повторный разбор той же пары не тратит LLM.',
    ),
    'linker_llm_base_url': (
        'URL LLM-провайдера L2',
        'Адрес OpenAI-совместимого API для LLM-вердиктов. Пусто = L2-автоматика отключена (очередь копится для ручного разбора). Изменение требует пересоздания клиента (рестарт).',
    ),
    'linker_llm_model': (
        'Модель L2-вердикта',
        'Имя модели LLM для вердиктов серой зоны. Применяется при рестарте (пересоздание клиента).',
    ),
    'linker_llm_timeout': (
        'Таймаут LLM (сек)',
        'Сколько секунд ждётся ответ LLM на один вердикт. Применяется при рестарте.',
    ),
    'linker_llm_retries': (
        'Ретраев на LLM-запрос',
        'Сколько повторных попыток делается при сбое LLM-запроса. Применяется при рестарте.',
    ),
    # ── §2.6 Рёбра графа ──
    'edge_lifecycle_enabled': (
        'Жизнь рёбер: мастер-флаг',
        'Включает цикл жизни рёбер: затухание неиспользуемых связей, усиление используемых, отсечение мёртвых. False — всё молчит (до стенд-репетиции).',
    ),
    'edge_reinforcement_enabled': (
        'Усиление рёбер',
        'Касание ребра при использовании увеличивает его вес (w += (1−w)×α) — частые связи крепнут. Работает только при включённой жизни рёбер.',
    ),
    'edge_decay_lambda': (
        'Скорость затухания рёбер',
        'λ в формуле w_eff = w·exp(−λ·дней): 0.02 ≈ ребро без использования теряет ~2% веса в день.',
    ),
    'edge_decay_lambda_min': (
        'Насыщение частых рёбер',
        'Нижний предел эффективной λ: часто используемые ребра затухают медленнее (λ/(1+used_count), но не ниже предела).',
    ),
    'edge_decay_floor': (
        'Порог отсечения ребра',
        'Эффективный вес ниже порога → ребро-кандидат на отсечение кампанией (пишется pruned_at, не DELETE).',
    ),
    'edge_prune_min_age_days': (
        'Возраст отсечения',
        'Кандидат на отсечение — ребро старше этого возраста (молодые связи дают шанс проявиться).',
    ),
    'edge_prune_dry_run': (
        'Отсечение: сухой режим',
        'True — кампания только считает кандидатов и пишет отчёт. False — боевой режим: рёбра помечаются pruned_at. Включать после ревизии отчёта.',
    ),
    'edge_reinforce_alpha': (
        'Сила касания',
        'Насколько одно использование подтягивает вес ребра: w += (1−w)×α, cap 1.0.',
    ),
    'edge_reinforce_flow_min': (
        'Порог потока касания',
        'При ассоциативном поиске ребро считается «использованным», если через него прошёл поток ≥ порога и оба конца в топ-K выдачи.',
    ),
    'traverse_activation_enabled': (
        'Ассоциативный обход графа',
        'Включает strategy="activation" обхода и поиска: PPR-распространение по живому графу. До включения — внятная ошибка вместо тихого fallback.',
    ),
    'ppr_damping': (
        'Демпфинг PPR',
        'Вероятность продолжить блуждание по графу на каждом шаге Personalized PageRank. Классика 0.85.',
    ),
    'traverse_activation_iterations': (
        'Итераций PPR',
        'Число итераций power iteration. 25 даёт точность топ-3 ±0.02 (0.85^25≈0.017); больше — точнее, дольше (~+1.1 мс/106k рёбер за 25).',
    ),
    'traverse_activation_top_k': (
        'Топ-K активации',
        'Сколько узлов возвращается ассоциативным расширением сверх seed-выдачи.',
    ),
    'traverse_symmetric_link_types': (
        'Симметричные типы связей',
        'Типы рёбер, по которым PPR ходит в обе стороны (related_to не имеет стрелки). Направленные (depends_on, contradicts, supersedes...) не симметрируются.',
    ),
    'traverse_max_nodes': (
        'Кап обхода графа',
        'Жёсткий предел узлов одного обхода графа — защита от тяжёлых запросов (Фаза 1.5).',
    ),
    # ── §2.7 Облачко знаний ──
    'context_cache_ttl': (
        'TTL облачка (сек)',
        'Время жизни Redis-кеша снапшота «облачка знаний» проекта и dirty-флага. Держать ≥ периода beat-пересборки rebuild_contexts.',
    ),
    'cloud_recency_half_life_days': (
        'Полураспад свежести облачка',
        'За сколько дней гранула теряет половину веса при отборе кандидатов в облачко — свежие решения всплывают над древними.',
    ),
    # ── §2.8 Карта ──
    'map_layout_bbox': (
        'Полусторона куба карты',
        'Координаты узлов нормируются в куб [−bbox, +bbox]³. Задаёт масштаб 3D-карты.',
    ),
    'map_min_dist': (
        'Мин. дистанция узлов',
        'Сила расталкивания пар узлов при релаксации (в единицах bbox) — узлы не слипаются.',
    ),
    'map_relax_iterations': (
        'Итераций релаксации',
        'Итерации раскладки с ранним выходом при стабилизации. Больше — ровнее карта, дольше сборка.',
    ),
    'map_drl_timeout': (
        'Таймаут DrL (сек)',
        'Лимит субпроцесса алгоритма DrL — сегфолт-щит (фикс F1): не уложился — откат на сферу.',
    ),
    'map_meta_ttl': (
        'TTL меты карты (сек)',
        'Время жизни Redis-кеша меты карты — цель «<50 мс на запрос».',
    ),
    'map_snapshot_ttl': (
        'TTL снапшота карты (сек)',
        'Время жизни gzip-снапшота текущей версии карты в Redis (сутки).',
    ),
    'map_stale_ttl': (
        'TTL устаревших снапшотов (сек)',
        'Сколько секунд держится устаревшая версия снапшота после выхода новой.',
    ),
    'map_build_wait_seconds': (
        'Ожидание сборки (сек)',
        'Сколько запрос ждёт конкурента под build-lock, прежде чем отдать предыдущий снапшот.',
    ),
    'map_preview_chars': (
        'Длина превью узла',
        'Сколько символов контента гранулы попадает в preview узла карты (обрезка по границе слова + «…»).',
    ),
    'map_name_chars': (
        'Длина имени узла',
        'Обрезка entity_name для тултипа узла.',
    ),
    'galactic_max_nodes': (
        'Лимит узлов Galactic',
        'Защитный порог масштаба раскладки (прод-OOM 20.09: пик >3 ГБ при лимите 512M): выше — раскладка пропускается с WARNING, карта остаётся на сфере.',
    ),
    'galactic_max_edges': (
        'Лимит рёбер Galactic',
        'Аналогично узлам: предел числа рёбер для боевой раскладки.',
    ),
    'galactic_max_clusters': (
        'Лимит кластеров Galactic',
        'Предел числа кластеров в раскладке.',
    ),
    # ── §2.9 Планировщик: воркер ──
    'celery_worker_concurrency': (
        'Процессов воркера',
        'Сколько задач воркер исполняет параллельно. Больше — выше пропускная способность, больше память (лимит контейнера 512M!).',
    ),
    'celery_worker_prefetch_multiplier': (
        'Предвыборка задач',
        'Сколько задач воркер берёт себе впрок. 1 = честное распределение между воркерами (fairness).',
    ),
    'celery_worker_max_tasks_per_child': (
        'Задач до перезапуска чилда',
        'Воркер-процесс перезапускается после стольких задач — защита от утечек памяти.',
    ),
    'celery_worker_max_memory_per_child': (
        'Память чилда до перезапуска (КБ)',
        'Перезапуск воркер-процесса при достижении этого RSS (200 МБ) — вторая ступень OOM-защиты.',
    ),
    'task_soft_time_limit': (
        'Soft-лимит задачи (сек)',
        'Секунды до SoftTimeLimitExceeded — задача получает шанс корректно завершиться.',
    ),
    'task_time_limit': (
        'Hard-лимит задачи (сек)',
        'Жёсткое убийство задачи по таймауту. Должен быть > soft-лимита.',
    ),
    'task_default_retry_delay': (
        'Задержка ретрая (сек)',
        'Базовая пауза перед повтором упавшей задачи (задачи могут переопределять).',
    ),
    'task_max_retries': (
        'Максимум ретраев',
        'Сколько раз упавшая задача повторяется по умолчанию.',
    ),
    'result_expires': (
        'Жизнь результатов (сек)',
        'Сколько секунд в Redis хранятся результаты задач, потом чистятся автоматически.',
    ),
    # ── §2.10 Планировщик: beat-расписания ──
    'schedule.update_worker_stats': (
        'Статистика воркера',
        'Как часто собирается статистика процессов воркера (метрики Мая).',
    ),
    'schedule.update_business_metrics': (
        'Бизнес-метрики',
        'Период пересчёта агрегированных бизнес-метрик.',
    ),
    'schedule.rebuild_contexts': (
        'Пересборка облачков',
        'Как часто переписываются грязные снапшоты «облачка знаний». Держать ≤ context_cache_ttl.',
    ),
    'schedule.refresh_clusters': (
        'Пересчёт кластеров',
        'Ежедневная разметка кластеров (после неё идут decay и отсечения).',
    ),
    'schedule.layout_map': (
        'Раскладка карты',
        'Ежедневный инкремент galactic_layout новых гранул (сразу после кластеров).',
    ),
    'schedule.confidence_decay': (
        'Затухание уверенности',
        'Ежедневное физическое затухание confidence по неймспейсам.',
    ),
    'schedule.edge_prune': (
        'Отсечение рёбер',
        'Ежедневная кампания жизни рёбер (после decay, до mark-stale).',
    ),
    'schedule.mark_stale': (
        'Пометка устаревших',
        'Ежедневная пометка заброшенных гранул по порогам stale_*.',
    ),
    'schedule.gc_superseded': (
        'GC версий',
        'Еженедельная сборка мусора superseded-версий (воскресенье, низкая нагрузка).',
    ),
    'schedule.orphans_cleanup': (
        'Чистка сирот',
        'Еженедельная чистка сиротских сущностей.',
    ),
    'schedule.linker_name_reconciler': (
        'Резолв имён',
        'Как часто кампания подшивает «висячие» ссылки к реальным гранулам.',
    ),
    'schedule.linker_co_occurrence': (
        'Co-occurrence-слой',
        'Как часто пересчитывается слой L1c (не чаще раза в час, ADR-019 C L3).',
    ),
    'schedule.linker_l2_verdicts': (
        'L2-вердикты',
        'Как часто воркер разбирает очередь серой зоны (очередь маленькая — можно часто).',
    ),
    # ── §2.11 Лимиты API ──
    'max_search_limit': (
        'Кап размера выдачи',
        'Жёсткий потолок `limit` поискового запроса через REST — защита REST-слоя от тяжёлых выборок.',
    ),
    'max_graph_depth': (
        'Кап глубины графа',
        'Максимальная глубина обхода графа через REST.',
    ),
}


def _build_registry() -> dict[str, SettingSpec]:
    """Все 97 runtime-ключей; русские подписи — из _RU_TEXTS (дословно 027).

    Отсутствие ключа в _RU_TEXTS — KeyError на импорте (громко, не тихо).
    """
    specs = [
        # ── search (10) ──
        SettingSpec("search_default_threshold", "float", "search", widget="slider_number", min_value=0.0, max_value=1.0),
        SettingSpec("hybrid_search_enabled", "bool", "search", widget="switch"),
        SettingSpec("hybrid_prefetch", "int", "search", min_value=10, max_value=1000),
        SettingSpec("rrf_k", "int", "search", min_value=1, max_value=500),
        SettingSpec("mmr_lambda", "float", "search", widget="slider_number", min_value=0.0, max_value=1.0),
        SettingSpec("recency_decay_rate", "float", "search", widget="slider_number", min_value=0.9, max_value=1.0),
        SettingSpec("recency_decay_rates", "json", "search", widget="kv_table"),
        SettingSpec("importance_multipliers", "json", "search", widget="kv_table"),
        SettingSpec("search_activation_enabled", "bool", "search", widget="switch"),
        SettingSpec("search_activation_seed_limit", "int", "search", min_value=4, max_value=30),
        # ── dedup (3) ──
        SettingSpec("dedup_enabled", "bool", "dedup", widget="switch", dangerous=True),
        SettingSpec("dedup_threshold", "float", "dedup", widget="slider_number", min_value=0.5, max_value=1.0),
        SettingSpec("dedup_thresholds", "json", "dedup", widget="kv_table"),
        # ── lifecycle (7) ──
        SettingSpec("supersession_confidence_factor", "float", "lifecycle", widget="slider_number", min_value=0.0, max_value=1.0),
        SettingSpec("confidence_decay_floor", "float", "lifecycle", widget="slider_number", min_value=0.0, max_value=0.5),
        SettingSpec("stale_threshold", "float", "lifecycle", widget="slider_number", min_value=0.0, max_value=1.0),
        SettingSpec("stale_days", "int", "lifecycle", min_value=7, max_value=365),
        SettingSpec("gc_purge_enabled", "bool", "lifecycle", widget="switch", dangerous=True),
        SettingSpec("gc_mode", "str", "lifecycle", widget="combobox", dangerous=True, enum_values=("disabled", "soft", "hard")),
        SettingSpec("gc_retention_days", "int", "lifecycle", dangerous=True, min_value=7, max_value=3650),
        # ── cluster (3) ──
        SettingSpec("cluster_threshold", "float", "cluster", widget="slider_number", min_value=0.5, max_value=1.0),
        SettingSpec("cluster_top_k", "int", "cluster", min_value=3, max_value=50),
        SettingSpec("cluster_min_members", "int", "cluster", min_value=2, max_value=10),
        # ── linker (20) ──
        SettingSpec("linker_enabled", "bool", "linker", widget="switch", dangerous=True),
        SettingSpec("linker_l1a_enabled", "bool", "linker", widget="switch"),
        SettingSpec("linker_l1c_enabled", "bool", "linker", widget="switch"),
        SettingSpec("linker_l2_manual", "bool", "linker", widget="switch"),
        SettingSpec("linker_synonym_threshold", "float", "linker", widget="slider_number", min_value=0.5, max_value=0.95),
        SettingSpec("linker_verdict_threshold", "float", "linker", widget="slider_number", min_value=0.7, max_value=0.99),
        SettingSpec("linker_ann_limit", "int", "linker", min_value=3, max_value=50),
        SettingSpec("linker_top_k", "int", "linker", min_value=1, max_value=20),
        SettingSpec("linker_cooccurrence_cap", "int", "linker", min_value=1, max_value=100),
        SettingSpec("linker_reconciler_batch", "int", "linker", min_value=50, max_value=5000),
        SettingSpec("linker_reconciler_dry_run", "bool", "linker", widget="switch", dangerous=True),
        SettingSpec("linker_l2_batch", "int", "linker", min_value=1, max_value=200),
        SettingSpec("linker_l2_max_attempts", "int", "linker", min_value=1, max_value=10),
        SettingSpec("linker_l1c_gate_min", "float", "linker", widget="slider_number", min_value=0.0, max_value=0.9),
        SettingSpec("linker_l1c_prune_batch", "int", "linker", min_value=32, max_value=2048),
        SettingSpec("linker_verdict_cache_ttl", "int", "linker", min_value=3600, max_value=7776000),
        # LLM-провайдер L2: применяется при рестарте (пересоздание клиента)
        SettingSpec("linker_llm_base_url", "str", "linker", widget="text", requires_restart=True),
        SettingSpec("linker_llm_model", "str", "linker", widget="text", requires_restart=True),
        SettingSpec("linker_llm_timeout", "float", "linker", requires_restart=True, min_value=1.0, max_value=120.0),
        SettingSpec("linker_llm_retries", "int", "linker", requires_restart=True, min_value=0, max_value=5),
        # ── edge (15) ──
        SettingSpec("edge_lifecycle_enabled", "bool", "edge", widget="switch", dangerous=True),
        SettingSpec("edge_reinforcement_enabled", "bool", "edge", widget="switch"),
        SettingSpec("edge_decay_lambda", "float", "edge", widget="slider_number", min_value=0.0, max_value=1.0),
        SettingSpec("edge_decay_lambda_min", "float", "edge", widget="slider_number", min_value=0.0, max_value=0.1),
        SettingSpec("edge_decay_floor", "float", "edge", widget="slider_number", min_value=0.0, max_value=0.5),
        SettingSpec("edge_prune_min_age_days", "int", "edge", min_value=7, max_value=365),
        SettingSpec("edge_prune_dry_run", "bool", "edge", widget="switch", dangerous=True),
        SettingSpec("edge_reinforce_alpha", "float", "edge", widget="slider_number", min_value=0.0, max_value=1.0),
        SettingSpec("edge_reinforce_flow_min", "float", "edge", widget="slider_number", min_value=0.0, max_value=0.1),
        SettingSpec("traverse_activation_enabled", "bool", "edge", widget="switch"),
        SettingSpec("ppr_damping", "float", "edge", widget="slider_number", min_value=0.5, max_value=0.99),
        SettingSpec("traverse_activation_iterations", "int", "edge", min_value=5, max_value=100),
        SettingSpec("traverse_activation_top_k", "int", "edge", min_value=10, max_value=500),
        SettingSpec("traverse_symmetric_link_types", "json", "edge", widget="checkboxes"),
        SettingSpec("traverse_max_nodes", "int", "edge", min_value=50, max_value=5000),
        # ── cloud (2) ──
        SettingSpec("context_cache_ttl", "int", "cloud", min_value=60, max_value=86400),
        SettingSpec("cloud_recency_half_life_days", "int", "cloud", min_value=7, max_value=365),
        # ── map (13) ──
        SettingSpec("map_layout_bbox", "int", "map", min_value=100, max_value=10000),
        SettingSpec("map_min_dist", "float", "map", min_value=1.0, max_value=500.0),
        SettingSpec("map_relax_iterations", "int", "map", min_value=1, max_value=100),
        SettingSpec("map_drl_timeout", "float", "map", min_value=10.0, max_value=600.0),
        SettingSpec("map_meta_ttl", "int", "map", min_value=5, max_value=3600),
        SettingSpec("map_snapshot_ttl", "int", "map", min_value=600, max_value=604800),
        SettingSpec("map_stale_ttl", "int", "map", min_value=30, max_value=86400),
        SettingSpec("map_build_wait_seconds", "float", "map", min_value=5.0, max_value=600.0),
        SettingSpec("map_preview_chars", "int", "map", min_value=40, max_value=1000),
        SettingSpec("map_name_chars", "int", "map", min_value=20, max_value=300),
        SettingSpec("galactic_max_nodes", "int", "map", min_value=1000, max_value=200000),
        SettingSpec("galactic_max_edges", "int", "map", min_value=1000, max_value=2000000),
        SettingSpec("galactic_max_clusters", "int", "map", min_value=100, max_value=50000),
        # ── celery (9): применяются при старте worker; concurrency — налету
        # (pool_grow/pool_shrink broadcast, приказ Ф2)
        SettingSpec("celery_worker_concurrency", "int", "celery", min_value=1, max_value=8),
        SettingSpec("celery_worker_prefetch_multiplier", "int", "celery", requires_restart=True, min_value=1, max_value=10),
        SettingSpec("celery_worker_max_tasks_per_child", "int", "celery", requires_restart=True, min_value=100, max_value=100000),
        SettingSpec("celery_worker_max_memory_per_child", "int", "celery", requires_restart=True, min_value=50000, max_value=500000),
        SettingSpec("task_soft_time_limit", "int", "celery", requires_restart=True, min_value=30, max_value=3600),
        SettingSpec("task_time_limit", "int", "celery", requires_restart=True, min_value=60, max_value=7200),
        SettingSpec("task_default_retry_delay", "int", "celery", requires_restart=True, min_value=1, max_value=600),
        SettingSpec("task_max_retries", "int", "celery", requires_restart=True, min_value=0, max_value=20),
        SettingSpec("result_expires", "int", "celery", requires_restart=True, min_value=300, max_value=86400),
        # ── schedule (13): json {type: interval|crontab, ...}; дефолты реестра §2.10 ──
        SettingSpec("schedule.update_worker_stats", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "interval", "seconds": 30}),
        SettingSpec("schedule.update_business_metrics", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "interval", "seconds": 3600}),
        SettingSpec("schedule.rebuild_contexts", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "interval", "seconds": 3600}),
        SettingSpec("schedule.refresh_clusters", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "crontab", "minute": "0", "hour": "2"}),
        SettingSpec("schedule.layout_map", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "crontab", "minute": "30", "hour": "2"}),
        SettingSpec("schedule.confidence_decay", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "crontab", "minute": "0", "hour": "3"}),
        SettingSpec("schedule.edge_prune", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "crontab", "minute": "30", "hour": "3"}),
        SettingSpec("schedule.mark_stale", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "crontab", "minute": "0", "hour": "4"}),
        SettingSpec("schedule.gc_superseded", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "crontab", "minute": "0", "hour": "5", "day_of_week": "sun"}),
        SettingSpec("schedule.orphans_cleanup", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "crontab", "minute": "30", "hour": "5", "day_of_week": "sun"}),
        SettingSpec("schedule.linker_name_reconciler", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "interval", "seconds": 3600}),
        SettingSpec("schedule.linker_co_occurrence", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "interval", "seconds": 3600}),
        SettingSpec("schedule.linker_l2_verdicts", "json", "schedule", widget="text", requires_restart=True,
                    default={"type": "interval", "seconds": 300}),
        # ── api_caps (2) ──
        SettingSpec("max_search_limit", "int", "api_caps", min_value=10, max_value=500),
        SettingSpec("max_graph_depth", "int", "api_caps", min_value=1, max_value=20),
    ]
    return {
        spec.key: replace(
            spec, title_ru=_RU_TEXTS[spec.key][0], description_ru=_RU_TEXTS[spec.key][1]
        )
        for spec in specs
    }


REGISTRY: dict[str, SettingSpec] = _build_registry()
SCHEDULE_KEYS = frozenset(k for k in REGISTRY if k.startswith("schedule."))


def get_spec(key: str) -> SettingSpec | None:
    return REGISTRY.get(key)


def get_default(key: str) -> Any:
    """Дефолт ключа: поле Settings (единый источник с сидингом 027) либо
    явный дефолт реестра (schedule.*, нет полей в Settings)."""
    spec = REGISTRY.get(key)
    if spec is None:
        raise KeyError(key)
    if key.startswith("schedule."):
        return spec.default
    return getattr(settings, key)


# ════════════════════════ Env-детекция ════════════════════════


def compute_env_overrides(config: Settings | None = None) -> dict[str, Any]:
    """Env-оверрайды runtime-ключей (§1.2–1.3 реестра).

    Источник 1 — pydantic model_fields_set: runtime-ключ, чьё поле Settings
    задано в env/.env, жёстко блокирует БД-слой. Источник 2 — csv
    RUNTIME_ENV_OVERRIDES: ключ → env-переменная KEY.TO.UPPER(), значение
    парсится по типу реестра (механизм оператора для ключей без поля
    Settings). Неизвестный реестру ключ → WARN + игнор (не падаем).
    """
    cfg = config or settings
    overrides: dict[str, Any] = {}
    fields_set = cfg.model_fields_set
    for key in REGISTRY:
        if not key.startswith("schedule.") and key in fields_set:
            overrides[key] = getattr(cfg, key)

    csv = cfg.runtime_env_overrides.strip()
    if csv:
        for key in (part.strip() for part in csv.split(",") if part.strip()):
            spec = REGISTRY.get(key)
            if spec is None:
                logger.warning("runtime_env_overrides: unknown key ignored", extra={"key": key})
                continue
            raw = os.environ.get(key.upper())
            if raw is None:
                continue
            try:
                overrides[key] = _parse_env_value(spec, raw)
            except ValueError as exc:
                logger.warning(
                    "runtime_env_overrides: unparsable value ignored",
                    extra={"key": key, "error": str(exc)},
                )
    return overrides


def _parse_env_value(spec: SettingSpec, raw: str) -> Any:
    if spec.value_type in ("int", "float"):
        return int(raw) if spec.value_type == "int" else float(raw)
    if spec.value_type == "bool":
        lowered = raw.strip().lower()
        if lowered not in ("true", "false", "1", "0"):
            raise ValueError(f"not a bool: {raw!r}")
        return lowered in ("true", "1")
    if spec.value_type == "json":
        return json.loads(raw)
    return raw


# ════════════════════════ Валидация ════════════════════════


def validate_value(key: str, value: Any) -> Any:
    """Схемная валидация одного значения; возвращает нормализованное значение.

    Бросает SettingsValidationError (HTTP 400) с человекочитаемой причиной.
    """
    spec = REGISTRY.get(key)
    if spec is None:
        raise SettingsValidationError(f"unknown setting key: {key}")
    vt = spec.value_type
    if vt == "bool":
        if not isinstance(value, bool):
            raise SettingsValidationError(f"{key}: expected bool, got {type(value).__name__}")
        return value
    if vt == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError(f"{key}: expected int, got {type(value).__name__}")
    elif vt == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsValidationError(f"{key}: expected float, got {type(value).__name__}")
        value = float(value)
    elif vt == "str":
        if not isinstance(value, str):
            raise SettingsValidationError(f"{key}: expected str, got {type(value).__name__}")
    elif vt == "json":
        if key in _NAMESPACE_DICT_SPECS:
            value = _validate_namespace_dict(key, value)
        elif key == "traverse_symmetric_link_types":
            value = _validate_symmetric_types(value)
        elif key in SCHEDULE_KEYS:
            _validate_schedule_value(key, value)
        else:
            raise SettingsValidationError(f"{key}: json value not expected for this key")
        return value
    else:  # pragma: no cover - реестр закрыт пятью типами
        raise SettingsValidationError(f"{key}: unsupported value_type {vt}")

    if spec.min_value is not None and value < spec.min_value:
        raise SettingsValidationError(f"{key}: {value} < min {spec.min_value}")
    if spec.max_value is not None and value > spec.max_value:
        raise SettingsValidationError(f"{key}: {value} > max {spec.max_value}")
    if spec.enum_values is not None and value not in spec.enum_values:
        raise SettingsValidationError(f"{key}: {value!r} not in {list(spec.enum_values)}")
    _validate_str_key_rules(key, value)
    return value


def _validate_str_key_rules(key: str, value: str) -> None:
    if key == "linker_llm_base_url" and value and not value.startswith(("http://", "https://")):
        raise SettingsValidationError(f"{key}: URL must start with http:// or https:// (or be empty)")
    if key == "linker_llm_model" and not (1 <= len(value) <= 100):
        raise SettingsValidationError(f"{key}: length must be 1..100")


def _validate_namespace_dict(key: str, value: Any) -> dict[str, float]:
    lo, hi = _NAMESPACE_DICT_SPECS[key]
    if not isinstance(value, dict) or not all(isinstance(v, (int, float)) for v in value.values()):
        raise SettingsValidationError(f"{key}: expected object namespace → float")
    if "default" not in value:
        raise SettingsValidationError(f"{key}: mandatory 'default' key missing")
    for ns, v in value.items():
        if not (lo <= float(v) <= hi):
            raise SettingsValidationError(f"{key}[{ns}]: {v} outside {lo}..{hi}")
    return {ns: float(v) for ns, v in value.items()}


def _validate_symmetric_types(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SettingsValidationError("traverse_symmetric_link_types: expected array of strings")
    bad = sorted(set(value) - SYMMETRIC_LINK_TYPES)
    if bad:
        raise SettingsValidationError(
            f"traverse_symmetric_link_types: not symmetric link types: {bad}; "
            f"allowed: {sorted(SYMMETRIC_LINK_TYPES)}"
        )
    return list(value)


_CRONTAB_FIELDS = ("minute", "hour", "day_of_week", "day_of_month", "month_of_year")


def _validate_schedule_value(key: str, value: Any) -> None:
    """Схема §2.10 + финальная проверка конструктором celery crontab."""
    if not isinstance(value, dict):
        raise SettingsValidationError(f"{key}: expected schedule object")
    stype = value.get("type")
    if stype == "interval":
        seconds = value.get("seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            raise SettingsValidationError(f"{key}: interval requires int 'seconds'")
        if not (10 <= seconds <= 604800):
            raise SettingsValidationError(f"{key}: interval seconds must be 10..604800")
        unknown = set(value) - {"type", "seconds"}
        if unknown:
            raise SettingsValidationError(f"{key}: unknown interval fields: {sorted(unknown)}")
        return
    if stype == "crontab":
        unknown = set(value) - {"type", *_CRONTAB_FIELDS}
        if unknown:
            raise SettingsValidationError(f"{key}: unknown crontab fields: {sorted(unknown)}")
        for name in _CRONTAB_FIELDS:
            raw = value.get(name)
            if raw is not None and not isinstance(raw, str):
                raise SettingsValidationError(f"{key}: crontab field {name} must be string or null")
        from celery.schedules import crontab

        try:
            crontab(
                minute=value.get("minute", "*"),
                hour=value.get("hour", "*"),
                day_of_week=value.get("day_of_week") or "*",
                day_of_month=value.get("day_of_month") or "*",
                month_of_year=value.get("month_of_year") or "*",
            )
        except ValueError as exc:
            raise SettingsValidationError(f"{key}: invalid crontab: {exc}") from exc
        return
    raise SettingsValidationError(f"{key}: type must be 'interval' or 'crontab'")


def validate_linker_invariant(values: Mapping[str, Any]) -> None:
    """Cross-field инвариант §2.5: synonym < verdict <= dedup_thresholds[ns].

    values — ИТОГОВОЕ состояние (overlay новых значений поверх effective).
    Нюанс: verdict == dedup[ns] допустим — L2-зона ns вырождается в пустую
    (пары уходят сразу в дедуп), это не захват дедуп-территории. Дефолты
    сами так живут: verdict 0.85 == dialogue_insights 0.85 (эскалация Ф2).
    """
    synonym = values.get("linker_synonym_threshold", get_default("linker_synonym_threshold"))
    verdict = values.get("linker_verdict_threshold", get_default("linker_verdict_threshold"))
    thresholds = values.get("dedup_thresholds", get_default("dedup_thresholds"))
    if synonym >= verdict:
        raise SettingsValidationError(
            f"linker invariant violated: linker_synonym_threshold ({synonym}) must be < "
            f"linker_verdict_threshold ({verdict})"
        )
    for ns, threshold in thresholds.items():
        if verdict > threshold:
            raise SettingsValidationError(
                f"linker invariant violated: linker_verdict_threshold ({verdict}) must be ≤ "
                f"dedup_thresholds[{ns}] ({threshold})"
            )


# ════════════════════════ Записи ════════════════════════


@dataclass
class SettingRecord:
    """Строка app_settings (метаданные сидинга 027 + текущее значение)."""

    key: str
    value: Any
    value_type: str
    group_key: str
    title_ru: str | None = None
    description_ru: str | None = None
    is_dangerous: bool = False
    requires_restart: bool = False
    updated_at: datetime | None = None
    updated_by: str = "seed"
    # Дефолт/схема — из реестра кода (источник истины при расхождении с сидингом)
    default_value: Any = None
    min_value: float | None = None
    max_value: float | None = None
    enum_values: list[str] | None = None
    widget: str = "number"


@dataclass
class ProfileRecord:
    """Профиль конфигурации (снапшот effective-значений)."""

    id: int
    name: str
    description: str | None
    is_builtin: bool
    created_at: datetime | None
    applied_at: datetime | None
    values: dict[str, Any]


_SELECT_SETTINGS = """
SELECT key, value, value_type, group_key, title_ru, description_ru,
       is_dangerous, requires_restart, updated_at, updated_by
FROM app_settings
"""


def _row_to_record(row: asyncpg.Record) -> SettingRecord:
    spec = REGISTRY.get(row["key"])
    return SettingRecord(
        key=row["key"],
        value=row["value"],
        value_type=spec.value_type if spec else row["value_type"],
        group_key=spec.group if spec else row["group_key"],
        # Пустые тексты в БД (строки, пересозданные upsert'ом до переноса
        # текстов в реестр кода) закрываются подписью спека — гигиена чтения
        title_ru=row["title_ru"] or (spec.title_ru if spec else ""),
        description_ru=row["description_ru"] or (spec.description_ru if spec else ""),
        is_dangerous=spec.dangerous if spec else row["is_dangerous"],
        requires_restart=spec.requires_restart if spec else row["requires_restart"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
        default_value=get_default(row["key"]) if spec else row["value"],
        min_value=spec.min_value if spec else None,
        max_value=spec.max_value if spec else None,
        enum_values=list(spec.enum_values) if spec and spec.enum_values else None,
        widget=spec.widget if spec else "number",
    )


_UPSERT_SQL = """
    INSERT INTO app_settings
        (key, value, value_type, group_key, title_ru, description_ru,
         default_value, min_value, max_value, enum_values,
         is_dangerous, requires_restart, updated_at, updated_by)
    VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, now(), $13)
    ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by
"""


def _upsert_args(key: str, value: Any, updated_by: str) -> tuple:
    """Аргументы upsert: ПОЛНАЯ строка метаданных из реестра кода.

    Колонки таблицы 027 NOT NULL — голый INSERT (key, value) падает
    NotNullViolation (сценарий: reset удалил строку → PUT того же ключа).
    Русские подписи — из SettingSpec (дословно сидинг 027): строка,
    пересозданная после reset, неотличима от сидированной (решение
    Мастера по компромиссу Ф4 — вариант «тексты в реестр кода»).

    Значения — python-объектами: jsonb-кодек пула (db/pool.py) сериализует
    сам; json.dumps здесь дал бы двойное кодирование (в БД ложилась бы
    JSON-строка вместо значения — паттерн insert_batch в pg_repository).
    """
    spec = REGISTRY[key]
    return (
        key,
        value,
        spec.value_type,
        spec.group,
        spec.title_ru,
        spec.description_ru,
        get_default(key),
        spec.min_value,
        spec.max_value,
        list(spec.enum_values) if spec.enum_values else None,
        spec.dangerous,
        spec.requires_restart,
        updated_by,
    )


class SettingsRepository:
    """CRUD app_settings + профили + валидация записи (миграция 027).

    Работает через asyncpg pool (jsonb-codec настроен в db.pool). Схема
    валидации — реестр кода; env-политика — env_locked_keys (вычислено
    compute_env_overrides, без цикла зависимостей на RuntimeConfig).
    """

    def __init__(self, pool: asyncpg.Pool, env_locked_keys: Iterable[str] = ()) -> None:
        self._pool = pool
        self._env_locked = frozenset(env_locked_keys)

    # ── Чтение ──

    async def load_all(self) -> dict[str, SettingRecord]:
        """Все строки app_settings. Пустая таблица/отсутствие — {} (дефолты)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_SETTINGS + " ORDER BY group_key, key")
        return {row["key"]: _row_to_record(row) for row in rows}

    async def get(self, key: str) -> SettingRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_SELECT_SETTINGS + " WHERE key = $1", key)
        return _row_to_record(row) if row else None

    # ── Запись ──

    async def set(
        self,
        key: str,
        value: Any,
        confirm: bool = False,
        updated_by: str = "ui",
        effective: Mapping[str, Any] | None = None,
    ) -> SettingRecord:
        """Записать значение: env-lock → 409, схема → 400, dangerous без
        confirm → 409, инвариант линкера → 400 (на итоговом состоянии)."""
        if key in self._env_locked:
            raise SettingsLockedError(f"{key} is managed via env/compose and locked")
        normalized = validate_value(key, value)
        spec = REGISTRY[key]
        if spec.dangerous and not confirm:
            raise SettingsConfirmationError(f"{key} is dangerous: confirm=true required", [key])
        overlay = dict(effective or {})
        overlay[key] = normalized
        validate_linker_invariant(overlay)

        async with self._pool.acquire() as conn:
            await conn.execute(_UPSERT_SQL, *_upsert_args(key, normalized, updated_by))
        record = await self.get(key)
        assert record is not None  # строка только что записана
        return record

    async def reset(self, key: str, confirm: bool = False) -> None:
        """Сброс к дефолту: удалить строку из БД (§1: сброс dangerous — с confirm)."""
        if key in self._env_locked:
            raise SettingsLockedError(f"{key} is managed via env/compose and locked")
        spec = REGISTRY.get(key)
        if spec is None:
            raise SettingsValidationError(f"unknown setting key: {key}")
        if spec.dangerous and not confirm:
            raise SettingsConfirmationError(f"{key} is dangerous: confirm=true required", [key])
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM app_settings WHERE key = $1", key,
            )

    async def reset_all(self, confirm: bool = False) -> list[str]:
        """Сброс всех переопределённых БД-ключей к дефолтам.

        Возвращает сброшенные ключи. Опасные ключи среди них без confirm →
        409 со списком (ничего не сбрасывается).
        """
        records = await self.load_all()
        # Мусорные строки (ключ удалён из реестра, но жив в БД) сбрасываем,
        # но в dangerous-проверку не тянем — REGISTRY[key] не существует
        resettable = [k for k in records if k not in self._env_locked]
        dangerous = [k for k in resettable if k in REGISTRY and REGISTRY[k].dangerous]
        if dangerous and not confirm:
            raise SettingsConfirmationError(
                "reset-all touches dangerous settings: confirm=true required", dangerous
            )
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "DELETE FROM app_settings WHERE key = $1", [(k,) for k in resettable]
            )
        return resettable

    # ── Профили ──

    async def list_profiles(self) -> list[ProfileRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, description, is_builtin, created_at, applied_at, values
                FROM app_settings_profiles ORDER BY created_at, id
                """
            )
        return [
            ProfileRecord(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                is_builtin=row["is_builtin"],
                created_at=row["created_at"],
                applied_at=row["applied_at"],
                values=row["values"] or {},
            )
            for row in rows
        ]

    async def get_profile(self, profile_id: int) -> ProfileRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, description, is_builtin, created_at, applied_at, values
                FROM app_settings_profiles WHERE id = $1
                """,
                profile_id,
            )
        if row is None:
            return None
        return ProfileRecord(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            is_builtin=row["is_builtin"],
            created_at=row["created_at"],
            applied_at=row["applied_at"],
            values=row["values"] or {},
        )

    async def create_profile(self, name: str, description: str | None, values: Mapping[str, Any]) -> ProfileRecord:
        """Снапшот значений: только ключи реестра (схему проверяет set-путь)."""
        filtered = {k: v for k, v in values.items() if k in REGISTRY}
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO app_settings_profiles (name, description, values)
                    VALUES ($1, $2, $3::jsonb)
                    RETURNING id, name, description, is_builtin, created_at, applied_at, values
                    """,
                    name,
                    description,
                    filtered,  # python-объект: jsonb-кодек пула сериализует сам
                )
            except asyncpg.UniqueViolationError as exc:
                raise ProfileError(f"profile name already exists: {name}") from exc
        assert row is not None
        return ProfileRecord(
            id=row["id"], name=row["name"], description=row["description"],
            is_builtin=row["is_builtin"], created_at=row["created_at"],
            applied_at=row["applied_at"], values=row["values"] or {},
        )

    async def update_profile(self, profile_id: int, values: Mapping[str, Any], description: str | None = None) -> ProfileRecord:
        """Перезаписать снапшот профиля текущими значениями."""
        existing = await self.get_profile(profile_id)
        if existing is None:
            raise ProfileNotFoundError(f"profile not found: {profile_id}")
        if existing.is_builtin:
            raise ProfileError("builtin profile cannot be modified")
        filtered = {k: v for k, v in values.items() if k in REGISTRY}
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE app_settings_profiles
                SET values = $2::jsonb, description = COALESCE($3, description)
                WHERE id = $1
                RETURNING id, name, description, is_builtin, created_at, applied_at, values
                """,
                profile_id,
                filtered,  # python-объект: jsonb-кодек пула сериализует сам
                description,
            )
        assert row is not None
        return ProfileRecord(
            id=row["id"], name=row["name"], description=row["description"],
            is_builtin=row["is_builtin"], created_at=row["created_at"],
            applied_at=row["applied_at"], values=row["values"] or {},
        )

    async def delete_profile(self, profile_id: int) -> None:
        existing = await self.get_profile(profile_id)
        if existing is None:
            raise ProfileNotFoundError(f"profile not found: {profile_id}")
        if existing.is_builtin:
            raise ProfileError("builtin profile cannot be deleted")
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM app_settings_profiles WHERE id = $1", profile_id)

    async def apply_profile(
        self,
        profile_id: int,
        confirm: bool = False,
        updated_by: str = "ui",
        effective: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Атомарно применить профиль: вся валидация до транзакции, записи —
        в одной транзакции. Env-ключи пропускаются (env жёстко сильнее),
        dangerous без confirm → 409 со списком. Возвращает отчёт."""
        profile = await self.get_profile(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"profile not found: {profile_id}")

        applicable: dict[str, Any] = {}
        skipped_env: list[str] = []
        for key, value in profile.values.items():
            if key in self._env_locked:
                skipped_env.append(key)
                continue
            if key not in REGISTRY:
                continue
            applicable[key] = validate_value(key, value)

        dangerous = sorted(k for k in applicable if REGISTRY[k].dangerous)
        if dangerous and not confirm:
            raise SettingsConfirmationError(
                "profile applies dangerous settings: confirm=true required", dangerous
            )
        overlay = dict(effective or {})
        overlay.update(applicable)
        validate_linker_invariant(overlay)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for key, value in applicable.items():
                    await conn.execute(_UPSERT_SQL, *_upsert_args(key, value, updated_by))
                await conn.execute(
                    "UPDATE app_settings_profiles SET applied_at = now() WHERE id = $1",
                    profile_id,
                )
        return {"applied": sorted(applicable), "skipped_env": sorted(skipped_env)}
