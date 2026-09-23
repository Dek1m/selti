// In-memory mock of the settings API. Каталог — бит-в-бит из
// docs/SETTINGS_REGISTRY.md (сидинг миграции 027, Сона); поведение
// повторяет контракт: 400 → {message, errors}, 409 → {message, keys},
// env-locked — read-only, reset/apply — с confirm. Секреты через API
// не приходят — в каталоге их нет by design (§3 реестра).

import { ApiError } from "./client";
import { validateValue, valuesEqual } from "./settings";
import type {
  ApplyProfileResult,
  SettingMeta,
  SettingsGroupInfo,
  SettingsPayload,
  SettingsProfile,
  ProfilesPayload,
  SettingWidget,
  SettingValueType,
} from "./types";

const LATENCY_MS = 120;
const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

interface MetaDraft {
  key: string;
  t: SettingValueType;
  g: string;
  title: string;
  desc: string;
  def: unknown;
  min?: number;
  max?: number;
  enum?: string[];
  danger?: boolean;
  restart?: boolean;
  widget: SettingWidget;
}

const CATALOG: MetaDraft[] = [
  // search — Поиск и ранжирование (§2.1)
  { key: "search_default_threshold", t: "float", g: "search", title: "Порог релевантности поиска", desc: "Минимальный балл схожести, ниже которого гранулы не попадают в выдачу, если клиент не задал свой порог.", def: 0.7, min: 0, max: 1, widget: "slider_number" },
  { key: "hybrid_search_enabled", t: "bool", g: "search", title: "Гибридный поиск", desc: "Включает гибридный поиск Фазы 1 (плотный вектор + полнотекстовый канал со слиянием RRF). Выключение откатывает на чистый векторный путь Qdrant — фича-флаг отката.", def: true, widget: "switch" },
  { key: "hybrid_prefetch", t: "int", g: "search", title: "Предвыборка кандидатов", desc: "Сколько кандидатов набирается в каждом канале до слияния RRF. Больше — точнее ранжирование, дороже запрос.", def: 100, min: 10, max: 1000, widget: "number" },
  { key: "rrf_k", t: "int", g: "search", title: "Коэффициент RRF", desc: "Константа сглаживания формулы слияния рангов: score = Σ 1/(k + rank). Меньше k — сильнее вес верхних позиций.", def: 60, min: 1, max: 500, widget: "number" },
  { key: "mmr_lambda", t: "float", g: "search", title: "Баланс MMR", desc: "Баланс релевантности и разнообразия выдачи при MMR-переранжировании: 1.0 — чистая релевантность, ниже — больше разнообразия.", def: 0.7, min: 0, max: 1, widget: "slider_number" },
  { key: "recency_decay_rate", t: "float", g: "search", title: "Затухание свежести (дефолт)", desc: "Ежедневный множитель веса гранулы в ранжировании для неймспейсов без своего override. 0.995 ≈ −0.5% в день.", def: 0.995, min: 0.9, max: 1, widget: "slider_number" },
  { key: "recency_decay_rates", t: "json", g: "search", title: "Затухание по неймспейсам", desc: "Индивидуальные скорости затухания свежести на неймспейс. Факты о пользователе живут дольше (0.999), инсайты разговоров устаревают быстрее (0.99). Ключи — namespace → float 0.9..1.0, обязательный «default».", def: { default: 0.995, user_facts: 0.999, project_meta: 0.998, code_knowledge: 0.995, dialogue_insights: 0.99, infrastructure: 0.993 }, widget: "kv_table" },
  { key: "importance_multipliers", t: "json", g: "search", title: "Приоритет неймспейсов", desc: "Множитель важности гранулы в ранжировании по её неймспейсу: факты пользователя (1.2) всплывают выше разговорного контента (0.8). Ключи — namespace → float 0.1..3.0, обязательный «default».", def: { default: 1, user_facts: 1.2, project_meta: 1.1, code_knowledge: 1, dialogue_insights: 0.8, infrastructure: 1 }, widget: "kv_table" },
  { key: "search_activation_enabled", t: "bool", g: "search", title: "Ассоциативный поиск", desc: "Включает стратегию activation тула memory_search: seed-гранулы расширяются соседями по графу связей (Personalized PageRank). До включения стратегия возвращает внятную ошибку.", def: false, widget: "switch" },
  { key: "search_activation_seed_limit", t: "int", g: "search", title: "Лимит seed-гранул", desc: "Сколько лучших прямых попаданий берётся как затравка для ассоциативного расширения.", def: 10, min: 4, max: 30, widget: "number" },
  // dedup — Дедупликация (§2.2)
  { key: "dedup_enabled", t: "bool", g: "dedup", title: "Дедупликация записей", desc: "При записи гранула сравнивается с существующими по смысловой близости; дубль не создаётся. Выключение допускает дубли — включать осознанно.", def: true, danger: true, widget: "switch" },
  { key: "dedup_threshold", t: "float", g: "dedup", title: "Порог дедупликации", desc: "Косинусная близость, выше которой новая гранула считается дублем существующей.", def: 0.95, min: 0.5, max: 1, widget: "slider_number" },
  { key: "dedup_thresholds", t: "json", g: "dedup", title: "Пороги по неймспейсам", desc: "Индивидуальные пороги дедупликации: для разговорных инсайтов планка ниже (0.85 — формулировки варьируются сильнее), для кода — выше. Ключи — namespace → float 0.5..1.0, обязательный «default».", def: { default: 0.95, user_facts: 0.9, dialogue_insights: 0.85, code_knowledge: 0.95, project_meta: 0.9, infrastructure: 0.95 }, widget: "kv_table" },
  // lifecycle — Жизненный цикл и GC (§2.3)
  { key: "supersession_confidence_factor", t: "float", g: "lifecycle", title: "Наследование уверенности", desc: "При создании новой версии гранулы уверенность наследуется с этим множителем (cap 0..1): каждое перепрохождение факта через систему стоит части уверенности.", def: 0.9, min: 0, max: 1, widget: "slider_number" },
  { key: "confidence_decay_floor", t: "float", g: "lifecycle", title: "Пол затухания уверенности", desc: "Ниже этого уровня ежедневное затухание останавливается: гранула не выродится в ноль, а станет кандидатом на ревизию (mark_stale).", def: 0.1, min: 0, max: 0.5, widget: "slider_number" },
  { key: "stale_threshold", t: "float", g: "lifecycle", title: "Порог устаревания", desc: "Уверенность ниже порога + нет доступа stale_days дней → гранула помечается устаревшей и попадает в очередь ревизии.", def: 0.3, min: 0, max: 1, widget: "slider_number" },
  { key: "stale_days", t: "int", g: "lifecycle", title: "Дней без доступа", desc: "Сколько дней гранула должна не запрашиваться, чтобы считаться заброшенной при упавшей уверенности.", def: 30, min: 7, max: 365, widget: "number" },
  { key: "gc_purge_enabled", t: "bool", g: "lifecycle", title: "Мастер-кран физического удаления", desc: "False — физическое удаление знаний невозможно в принципе (полная история сохраняется всегда). True ОТКРЫВАЕТ hard delete устаревших версий. Включать только осознанно после бэкапа.", def: false, danger: true, widget: "switch" },
  { key: "gc_mode", t: "str", g: "lifecycle", title: "Режим GC", desc: "disabled — ничего не удаляется (только отчёт кандидатов); hard — физическое удаление superseded-версий старше retention (работает только при включённом мастер-кране); soft — зарезервирован будущими фазами.", def: "disabled", enum: ["disabled", "soft", "hard"], danger: true, widget: "combobox" },
  { key: "gc_retention_days", t: "int", g: "lifecycle", title: "Срок хранения версий", desc: "Сколько дней после замены версии GC в режиме hard держит superseded-копию перед физическим удалением.", def: 90, min: 7, max: 3650, danger: true, widget: "number" },
  // cluster — Кластеризация (§2.4)
  { key: "cluster_threshold", t: "float", g: "cluster", title: "Порог близости кластеров", desc: "Минимальная близость эмбеддингов (score в Qdrant) для попадания соседа в кандидаты кластера при ночной разметке.", def: 0.92, min: 0.5, max: 1, widget: "slider_number" },
  { key: "cluster_top_k", t: "int", g: "cluster", title: "Соседей на гранулу", desc: "Сколько ближайших соседей рассматривается для каждой гранулы при сборке кластеров. Больше — крупнее кластеры, дольше расчёт.", def: 10, min: 3, max: 50, widget: "number" },
  { key: "cluster_min_members", t: "int", g: "cluster", title: "Минимум участников", desc: "Группы меньше этого размера кластером не считаются (остаются одиночными вершинами).", def: 2, min: 2, max: 10, widget: "number" },
  // linker — Линкер (§2.5)
  { key: "linker_enabled", t: "bool", g: "linker", title: "Автолинкинг", desc: "Мастер-выключатель автолинкинга новых гранул. Выключение останавливает построение новых связей знаний — включать осознанно.", def: true, danger: true, widget: "switch" },
  { key: "linker_l1a_enabled", t: "bool", g: "linker", title: "Слой L1a (синонимы)", desc: "Автосвязи related_to по ANN-поиску синонимов эмбеддингов. Выключается при риске шума связей (флаг отката ADR-019.1).", def: true, widget: "switch" },
  { key: "linker_l1c_enabled", t: "bool", g: "linker", title: "Слой L1c (совместные упоминания)", desc: "Связи между гранулами, встречавшимися в одном контексте (co-occurrence).", def: true, widget: "switch" },
  { key: "linker_l2_manual", t: "bool", g: "linker", title: "Ручной режим L2", desc: "True — «серую зону» близости разбирает человек-агент тулами memory_linker_review/verdict; False — очередь отдаётся LLM-воркеру. Режим назначен приказом Мастера 20.09.", def: true, widget: "switch" },
  { key: "linker_synonym_threshold", t: "float", g: "linker", title: "Порог синонимии L1a", desc: "Нижняя граница «серой зоны»: ниже — тишина (шум), выше начинается auto-related_to. Должен быть ниже вердиктного порога.", def: 0.8, min: 0.5, max: 0.95, widget: "slider_number" },
  { key: "linker_verdict_threshold", t: "float", g: "linker", title: "Порог LLM-вердикта", desc: "Верхняя граница auto-слоя: от этого порога до порога дедупликации пару связывает только явный вердикт (LLM или человек).", def: 0.85, min: 0.7, max: 0.99, widget: "slider_number" },
  { key: "linker_ann_limit", t: "int", g: "linker", title: "Соседей ANN на гранулу", desc: "Верхний кап кандидатов синонимии из векторного поиска на одну новую гранулу.", def: 10, min: 3, max: 50, widget: "number" },
  { key: "linker_top_k", t: "int", g: "linker", title: "Кандидатов в L2-промпте", desc: "Сколько пар-кандидатов попадает в один LLM-запрос вердикта.", def: 5, min: 1, max: 20, widget: "number" },
  { key: "linker_cooccurrence_cap", t: "int", g: "linker", title: "Кап co-occurrence рёбер", desc: "Максимум L1c-рёбер на гранулу (приоритет свежим соседям) — защита от разрастания графа.", def: 10, min: 1, max: 100, widget: "number" },
  { key: "linker_reconciler_batch", t: "int", g: "linker", title: "Батч резолва имён", desc: "Сколько «висячих» ссылок name_reconciler обрабатывает за итерацию кампании.", def: 500, min: 50, max: 5000, widget: "number" },
  { key: "linker_reconciler_dry_run", t: "bool", g: "linker", title: "Резолв имён: сухой режим", desc: "True — кампания только строит отчёт, ничего не переписывает. False — боевой резолв ссылок. Переключать после ручной проверки первого отчёта.", def: true, danger: true, widget: "switch" },
  { key: "linker_l2_batch", t: "int", g: "linker", title: "Размер L2-батча", desc: "Сколько элементов серой зоны обрабатывается за прогон воркера/агента.", def: 20, min: 1, max: 200, widget: "number" },
  { key: "linker_l2_max_attempts", t: "int", g: "linker", title: "Попыток L2-вердикта", desc: "Сколько раз сбойный элемент очереди возвращается в обработку, прежде чем отбрасывается с WARNING.", def: 3, min: 1, max: 10, widget: "number" },
  { key: "linker_l1c_gate_min", t: "float", g: "linker", title: "Гейт L1c по косинусу", desc: "Ребро co-occurrence живёт только при косинусной близости пары ≥ порога (одна сессия ≠ смысловая близость). 0.0 — гейт выключен.", def: 0.3, min: 0, max: 0.9, widget: "slider_number" },
  { key: "linker_l1c_prune_batch", t: "int", g: "linker", title: "Батч чистки L1c-истории", desc: "Размер пакета исторических пар при one-off кампании перепроверки co-occurrence гейтом.", def: 256, min: 32, max: 2048, widget: "number" },
  { key: "linker_verdict_cache_ttl", t: "int", g: "linker", title: "Кеш вердиктов (сек)", desc: "Сколько секунд хранится вердикт по паре (30 дней) — повторный разбор той же пары не тратит LLM.", def: 2592000, min: 3600, max: 7776000, widget: "number" },
  { key: "linker_llm_base_url", t: "str", g: "linker", title: "URL LLM-провайдера L2", desc: "Адрес OpenAI-совместимого API для LLM-вердиктов. Пусто = L2-автоматика отключена (очередь копится для ручного разбора). Изменение требует пересоздания клиента (рестарт).", def: "", restart: true, widget: "text" },
  { key: "linker_llm_model", t: "str", g: "linker", title: "Модель L2-вердикта", desc: "Имя модели LLM для вердиктов серой зоны. Применяется при рестарте (пересоздание клиента).", def: "glm-4.7-flash", restart: true, widget: "text" },
  { key: "linker_llm_timeout", t: "float", g: "linker", title: "Таймаут LLM (сек)", desc: "Сколько секунд ждётся ответ LLM на один вердикт. Применяется при рестарте.", def: 10, min: 1, max: 120, restart: true, widget: "number" },
  { key: "linker_llm_retries", t: "int", g: "linker", title: "Ретраев на LLM-запрос", desc: "Сколько повторных попыток делается при сбое LLM-запроса. Применяется при рестарте.", def: 1, min: 0, max: 5, restart: true, widget: "number" },
  // edge — Рёбра графа (§2.6)
  { key: "edge_lifecycle_enabled", t: "bool", g: "edge", title: "Жизнь рёбер: мастер-флаг", desc: "Включает цикл жизни рёбер: затухание неиспользуемых связей, усиление используемых, отсечение мёртвых. False — всё молчит (до стенд-репетиции).", def: false, danger: true, widget: "switch" },
  { key: "edge_reinforcement_enabled", t: "bool", g: "edge", title: "Усиление рёбер", desc: "Касание ребра при использовании увеличивает его вес (w += (1−w)×α) — частые связи крепнут. Работает только при включённой жизни рёбер.", def: true, widget: "switch" },
  { key: "edge_decay_lambda", t: "float", g: "edge", title: "Скорость затухания рёбер", desc: "λ в формуле w_eff = w·exp(−λ·дней): 0.02 ≈ ребро без использования теряет ~2% веса в день.", def: 0.02, min: 0, max: 1, widget: "slider_number" },
  { key: "edge_decay_lambda_min", t: "float", g: "edge", title: "Насыщение частых рёбер", desc: "Нижний предел эффективной λ: часто используемые ребра затухают медленнее (λ/(1+used_count), но не ниже предела).", def: 0.002, min: 0, max: 0.1, widget: "slider_number" },
  { key: "edge_decay_floor", t: "float", g: "edge", title: "Порог отсечения ребра", desc: "Эффективный вес ниже порога → ребро-кандидат на отсечение кампанией (пишется pruned_at, не DELETE).", def: 0.05, min: 0, max: 0.5, widget: "slider_number" },
  { key: "edge_prune_min_age_days", t: "int", g: "edge", title: "Возраст отсечения", desc: "Кандидат на отсечение — ребро старше этого возраста (молодые связи дают шанс проявиться).", def: 30, min: 7, max: 365, widget: "number" },
  { key: "edge_prune_dry_run", t: "bool", g: "edge", title: "Отсечение: сухой режим", desc: "True — кампания только считает кандидатов и пишет отчёт. False — боевой режим: рёбра помечаются pruned_at. Включать после ревизии отчёта.", def: true, danger: true, widget: "switch" },
  { key: "edge_reinforce_alpha", t: "float", g: "edge", title: "Сила касания", desc: "Насколько одно использование подтягивает вес ребра: w += (1−w)×α, cap 1.0.", def: 0.2, min: 0, max: 1, widget: "slider_number" },
  { key: "edge_reinforce_flow_min", t: "float", g: "edge", title: "Порог потока касания", desc: "При ассоциативном поиске ребро считается «использованным», если через него прошёл поток ≥ порога и оба конца в топ-K выдачи.", def: 0.001, min: 0, max: 0.1, widget: "slider_number" },
  { key: "traverse_activation_enabled", t: "bool", g: "edge", title: "Ассоциативный обход графа", desc: "Включает strategy=activation обхода и поиска: PPR-распространение по живому графу. До включения — внятная ошибка вместо тихого fallback.", def: false, widget: "switch" },
  { key: "ppr_damping", t: "float", g: "edge", title: "Демпфинг PPR", desc: "Вероятность продолжить блуждание по графу на каждом шаге Personalized PageRank. Классика 0.85.", def: 0.85, min: 0.5, max: 0.99, widget: "slider_number" },
  { key: "traverse_activation_iterations", t: "int", g: "edge", title: "Итераций PPR", desc: "Число итераций power iteration. 25 даёт точность топ-3 ±0.02 (0.85^25≈0.017); больше — точнее, дольше (~+1.1 мс/106k рёбер за 25).", def: 25, min: 5, max: 100, widget: "number" },
  { key: "traverse_activation_top_k", t: "int", g: "edge", title: "Топ-K активации", desc: "Сколько узлов возвращается ассоциативным расширением сверх seed-выдачи.", def: 50, min: 10, max: 500, widget: "number" },
  { key: "traverse_symmetric_link_types", t: "json", g: "edge", title: "Симметричные типы связей", desc: "Типы рёбер, по которым PPR ходит в обе стороны (related_to не имеет стрелки). Направленные (depends_on, contradicts, supersedes…) не симметрируются.", def: ["related_to"], enum: ["related_to"], widget: "checkboxes" },
  { key: "traverse_max_nodes", t: "int", g: "edge", title: "Кап обхода графа", desc: "Жёсткий предел узлов одного обхода графа — защита от тяжёлых запросов (Фаза 1.5).", def: 500, min: 50, max: 5000, widget: "number" },
  // cloud — Облачко знаний (§2.7)
  { key: "context_cache_ttl", t: "int", g: "cloud", title: "TTL облачка (сек)", desc: "Время жизни Redis-кеша снапшота «облачка знаний» проекта и dirty-флага. Держать ≥ периода beat-пересборки rebuild_contexts.", def: 3600, min: 60, max: 86400, widget: "number" },
  { key: "cloud_recency_half_life_days", t: "int", g: "cloud", title: "Полураспад свежести облачка", desc: "За сколько дней гранула теряет половину веса при отборе кандидатов в облачко — свежие решения всплывают над древними.", def: 30, min: 7, max: 365, widget: "number" },
  // map — Карта (§2.8)
  { key: "map_layout_bbox", t: "int", g: "map", title: "Полусторона куба карты", desc: "Координаты узлов нормируются в куб [−bbox, +bbox]³. Задаёт масштаб 3D-карты.", def: 1000, min: 100, max: 10000, widget: "number" },
  { key: "map_min_dist", t: "float", g: "map", title: "Мин. дистанция узлов", desc: "Сила расталкивания пар узлов при релаксации (в единицах bbox) — узлы не слипаются.", def: 50, min: 1, max: 500, widget: "number" },
  { key: "map_relax_iterations", t: "int", g: "map", title: "Итераций релаксации", desc: "Итерации раскладки с ранним выходом при стабилизации. Больше — ровнее карта, дольше сборка.", def: 8, min: 1, max: 100, widget: "number" },
  { key: "map_drl_timeout", t: "float", g: "map", title: "Таймаут DrL (сек)", desc: "Лимит субпроцесса алгоритма DrL — сегфолт-щит (фикс F1): не уложился — откат на сферу.", def: 120, min: 10, max: 600, widget: "number" },
  { key: "map_meta_ttl", t: "int", g: "map", title: "TTL меты карты (сек)", desc: "Время жизни Redis-кеша меты карты — цель «<50 мс на запрос».", def: 60, min: 5, max: 3600, widget: "number" },
  { key: "map_snapshot_ttl", t: "int", g: "map", title: "TTL снапшота карты (сек)", desc: "Время жизни gzip-снапшота текущей версии карты в Redis (сутки).", def: 86400, min: 600, max: 604800, widget: "number" },
  { key: "map_stale_ttl", t: "int", g: "map", title: "TTL устаревших снапшотов (сек)", desc: "Сколько секунд держится устаревшая версия снапшота после выхода новой.", def: 300, min: 30, max: 86400, widget: "number" },
  { key: "map_build_wait_seconds", t: "float", g: "map", title: "Ожидание сборки (сек)", desc: "Сколько запрос ждёт конкурента под build-lock, прежде чем отдать предыдущий снапшот.", def: 60, min: 5, max: 600, widget: "number" },
  { key: "map_preview_chars", t: "int", g: "map", title: "Длина превью узла", desc: "Сколько символов контента гранулы попадает в preview узла карты (обрезка по границе слова + «…»).", def: 180, min: 40, max: 1000, widget: "number" },
  { key: "map_name_chars", t: "int", g: "map", title: "Длина имени узла", desc: "Обрезка entity_name для тултипа узла.", def: 80, min: 20, max: 300, widget: "number" },
  { key: "galactic_max_nodes", t: "int", g: "map", title: "Лимит узлов Galactic", desc: "Защитный порог масштаба раскладки (прод-OOM 20.09: пик >3 ГБ при лимите 512M): выше — раскладка пропускается с WARNING, карта остаётся на сфере.", def: 20000, min: 1000, max: 200000, widget: "number" },
  { key: "galactic_max_edges", t: "int", g: "map", title: "Лимит рёбер Galactic", desc: "Аналогично узлам: предел числа рёбер для боевой раскладки.", def: 150000, min: 1000, max: 2000000, widget: "number" },
  { key: "galactic_max_clusters", t: "int", g: "map", title: "Лимит кластеров Galactic", desc: "Предел числа кластеров в раскладке.", def: 3000, min: 100, max: 50000, widget: "number" },
  // celery — Планировщик: воркер (§2.9); все ключи читаются при старте
  { key: "celery_worker_concurrency", t: "int", g: "celery", title: "Процессов воркера", desc: "Сколько задач воркер исполняет параллельно. Больше — выше пропускная способность, больше память (лимит контейнера 512M!). Меняется налету broadcast-ом pool_grow/shrink.", def: 4, min: 1, max: 8, widget: "number" },
  { key: "celery_worker_prefetch_multiplier", t: "int", g: "celery", title: "Предвыборка задач", desc: "Сколько задач воркер берёт себе впрок. 1 = честное распределение между воркерами (fairness).", def: 1, min: 1, max: 10, restart: true, widget: "number" },
  { key: "celery_worker_max_tasks_per_child", t: "int", g: "celery", title: "Задач до перезапуска чилда", desc: "Воркер-процесс перезапускается после стольких задач — защита от утечек памяти.", def: 1000, min: 100, max: 100000, restart: true, widget: "number" },
  { key: "celery_worker_max_memory_per_child", t: "int", g: "celery", title: "Память чилда до перезапуска (КБ)", desc: "Перезапуск воркер-процесса при достижении этого RSS (200 МБ) — вторая ступень OOM-защиты.", def: 200000, min: 50000, max: 500000, restart: true, widget: "number" },
  { key: "task_soft_time_limit", t: "int", g: "celery", title: "Soft-лимит задачи (сек)", desc: "Секунды до SoftTimeLimitExceeded — задача получает шанс корректно завершиться.", def: 240, min: 30, max: 3600, restart: true, widget: "number" },
  { key: "task_time_limit", t: "int", g: "celery", title: "Hard-лимит задачи (сек)", desc: "Жёсткое убийство задачи по таймауту. Должен быть > soft-лимита.", def: 300, min: 60, max: 7200, restart: true, widget: "number" },
  { key: "task_default_retry_delay", t: "int", g: "celery", title: "Задержка ретрая (сек)", desc: "Базовая пауза перед повтором упавшей задачи (задачи могут переопределять).", def: 30, min: 1, max: 600, restart: true, widget: "number" },
  { key: "task_max_retries", t: "int", g: "celery", title: "Максимум ретраев", desc: "Сколько раз упавшая задача повторяется по умолчанию.", def: 5, min: 0, max: 20, restart: true, widget: "number" },
  { key: "result_expires", t: "int", g: "celery", title: "Жизнь результатов (сек)", desc: "Сколько секунд в Redis хранятся результаты задач, потом чистятся автоматически.", def: 3600, min: 300, max: 86400, restart: true, widget: "number" },
  // schedule — Планировщик: beat-расписания (§2.10); JSON interval/crontab, все restart
  { key: "schedule.update_worker_stats", t: "json", g: "schedule", title: "Статистика воркера", desc: "Как часто собирается статистика процессов воркера (метрики Мая). Схема: {type: interval, seconds} или {type: crontab, minute, hour, day_of_week}.", def: { type: "interval", seconds: 30 }, restart: true, widget: "text" },
  { key: "schedule.update_business_metrics", t: "json", g: "schedule", title: "Бизнес-метрики", desc: "Период пересчёта агрегированных бизнес-метрик. Схема: interval-объект или crontab-объект.", def: { type: "interval", seconds: 3600 }, restart: true, widget: "text" },
  { key: "schedule.rebuild_contexts", t: "json", g: "schedule", title: "Пересборка облачков", desc: "Как часто переписываются грязные снапшоты «облачка знаний». Держать ≤ context_cache_ttl.", def: { type: "interval", seconds: 3600 }, restart: true, widget: "text" },
  { key: "schedule.refresh_clusters", t: "json", g: "schedule", title: "Пересчёт кластеров", desc: "Ежедневная разметка кластеров (после неё идут decay и отсечения).", def: { type: "crontab", minute: "0", hour: "2", day_of_week: null }, restart: true, widget: "text" },
  { key: "schedule.layout_map", t: "json", g: "schedule", title: "Раскладка карты", desc: "Ежедневный инкремент galactic_layout новых гранул (сразу после кластеров).", def: { type: "crontab", minute: "30", hour: "2", day_of_week: null }, restart: true, widget: "text" },
  { key: "schedule.confidence_decay", t: "json", g: "schedule", title: "Затухание уверенности", desc: "Ежедневное физическое затухание confidence по неймспейсам.", def: { type: "crontab", minute: "0", hour: "3", day_of_week: null }, restart: true, widget: "text" },
  { key: "schedule.edge_prune", t: "json", g: "schedule", title: "Отсечение рёбер", desc: "Ежедневная кампания жизни рёбер (после decay, до mark-stale).", def: { type: "crontab", minute: "30", hour: "3", day_of_week: null }, restart: true, widget: "text" },
  { key: "schedule.mark_stale", t: "json", g: "schedule", title: "Пометка устаревших", desc: "Ежедневная пометка заброшенных гранул по порогам stale_*.", def: { type: "crontab", minute: "0", hour: "4", day_of_week: null }, restart: true, widget: "text" },
  { key: "schedule.gc_superseded", t: "json", g: "schedule", title: "GC версий", desc: "Еженедельная сборка мусора superseded-версий (воскресенье, низкая нагрузка).", def: { type: "crontab", minute: "0", hour: "5", day_of_week: "sun" }, restart: true, widget: "text" },
  { key: "schedule.orphans_cleanup", t: "json", g: "schedule", title: "Чистка сирот", desc: "Еженедельная чистка сиротских сущностей.", def: { type: "crontab", minute: "30", hour: "5", day_of_week: "sun" }, restart: true, widget: "text" },
  { key: "schedule.linker_name_reconciler", t: "json", g: "schedule", title: "Резолв имён", desc: "Как часто кампания подшивает «висячие» ссылки к реальным гранулам.", def: { type: "interval", seconds: 3600 }, restart: true, widget: "text" },
  { key: "schedule.linker_co_occurrence", t: "json", g: "schedule", title: "Co-occurrence-слой", desc: "Как часто пересчитывается слой L1c (не чаще раза в час, ADR-019 C L3).", def: { type: "interval", seconds: 3600 }, restart: true, widget: "text" },
  { key: "schedule.linker_l2_verdicts", t: "json", g: "schedule", title: "L2-вердикты", desc: "Как часто воркер разбирает очередь серой зоны (очередь маленькая — можно часто).", def: { type: "interval", seconds: 300 }, restart: true, widget: "text" },
  // api_caps — Лимиты API (§2.11)
  { key: "max_search_limit", t: "int", g: "api_caps", title: "Кап размера выдачи", desc: "Жёсткий потолок limit поискового запроса через REST — защита REST-слоя от тяжёлых выборок. Превышение → 400 с именем ключа.", def: 100, min: 10, max: 500, widget: "number" },
  { key: "max_graph_depth", t: "int", g: "api_caps", title: "Кап глубины графа", desc: "Максимальная глубина обхода графа через REST. Превышение → 400 с именем ключа.", def: 10, min: 1, max: 20, widget: "number" },
];

/** Русские заголовки групп — приходят из API (сидинг); в моке повторены. */
const GROUPS: SettingsGroupInfo[] = [
  { key: "search", title_ru: "Поиск и ранжирование" },
  { key: "dedup", title_ru: "Дедупликация" },
  { key: "lifecycle", title_ru: "Жизненный цикл и GC" },
  { key: "cluster", title_ru: "Кластеризация" },
  { key: "linker", title_ru: "Линкер" },
  { key: "edge", title_ru: "Рёбра графа" },
  { key: "cloud", title_ru: "Облачко знаний" },
  { key: "map", title_ru: "Карта" },
  { key: "celery", title_ru: "Планировщик — воркер" },
  { key: "schedule", title_ru: "Планировщик — расписания" },
  { key: "api_caps", title_ru: "Лимиты API" },
];

/** Server-side mirror: value = effective, dbValue = сырое из БД. */
interface LiveSetting {
  meta: MetaDraft;
  value: unknown;
  dbValue: unknown;
  envLocked: boolean;
  updatedAt: string | null;
  updatedBy: string | null;
}

const BUILT = buildLive();

function buildLive(): Map<string, LiveSetting> {
  const m = new Map<string, LiveSetting>();
  for (const meta of CATALOG) {
    m.set(meta.key, { meta, value: structuredClone(meta.def), dbValue: null, envLocked: false, updatedAt: null, updatedBy: null });
  }
  return m;
}
function cloned(src: Map<string, LiveSetting>): Map<string, LiveSetting> {
  const m = new Map<string, LiveSetting>();
  for (const [k, v] of src) m.set(k, structuredClone(v));
  return m;
}

/** Эмуляция прода: env/compose-оверрайды (жёстко блокируют поле) и
 * записи в БД поверх сидинга. */
const ENV_OVERRIDES: Record<string, unknown> = {
  dedup_threshold: 0.93, // SELTI_DEDUP_THRESHOLD в compose
  traverse_max_nodes: 800, // SELTI_TRAVERSE_MAX_NODES
};
const DB_OVERRIDES: Array<{ key: string; value: unknown; at: string; by: string }> = [
  { key: "stale_threshold", value: 0.35, at: "2026-09-22T18:40:00Z", by: "Серёжа" },
  { key: "linker_l1c_gate_min", value: 0.35, at: "2026-09-21T10:15:00Z", by: "Афина" },
  { key: "ppr_damping", value: 0.88, at: "2026-09-20T09:05:00Z", by: "Серёжа" },
];

for (const [key, v] of Object.entries(ENV_OVERRIDES)) {
  const s = BUILT.get(key)!;
  s.value = v;
  s.envLocked = true;
}
for (const o of DB_OVERRIDES) {
  const s = BUILT.get(o.key)!;
  s.value = o.value;
  s.dbValue = o.value;
  s.updatedAt = o.at;
  s.updatedBy = o.by;
}

// ВАЖНО: live клонируется ПОСЛЕ применения оверрайдов к BUILT —
// иначе первый рендер (без mockResetState) отдаёт чистый сидинг.
let live: Map<string, LiveSetting> = cloned(BUILT);

/** Тестовый хелпер: вернуть мок к заводскому состоянию. */
export function mockResetState(): void {
  live = cloned(BUILT);
  profiles = new Map(DEFAULT_PROFILES.map((p) => [p.id, p]));
  snapshots = new Map();
}

function toMeta(s: LiveSetting): SettingMeta {
  return {
    key: s.meta.key,
    value: structuredClone(s.value),
    db_value: structuredClone(s.dbValue),
    value_type: s.meta.t,
    group: s.meta.g,
    title_ru: s.meta.title,
    description_ru: s.meta.desc,
    default_value: structuredClone(s.meta.def),
    ...(s.meta.min !== undefined ? { min_value: s.meta.min } : {}),
    ...(s.meta.max !== undefined ? { max_value: s.meta.max } : {}),
    ...(s.meta.enum ? { enum_values: s.meta.enum } : {}),
    is_dangerous: s.meta.danger ?? false,
    requires_restart: s.meta.restart ?? false,
    is_env_locked: s.envLocked,
    effective_source: s.envLocked ? "env" : s.dbValue !== null ? "db" : "default",
    differs_from_default: !valuesEqual(s.value, s.meta.def),
    widget: s.meta.widget,
    updated_at: s.updatedAt,
    updated_by: s.updatedBy,
  };
}

/** SettingMeta из сида (для серверной валидации значения до записи). */
function draftMeta(m: MetaDraft, value: unknown): SettingMeta {
  return {
    key: m.key,
    value: structuredClone(value),
    db_value: null,
    value_type: m.t,
    group: m.g,
    title_ru: m.title,
    description_ru: m.desc,
    default_value: structuredClone(m.def),
    ...(m.min !== undefined ? { min_value: m.min } : {}),
    ...(m.max !== undefined ? { max_value: m.max } : {}),
    ...(m.enum ? { enum_values: m.enum } : {}),
    is_dangerous: m.danger ?? false,
    requires_restart: m.restart ?? false,
    is_env_locked: false,
    effective_source: "default",
    differs_from_default: false,
    widget: m.widget,
    updated_at: null,
    updated_by: null,
  };
}

/* ── Профили: builtin «Заводские настройки» + пользовательские ── */

const BUILTIN_ID = "builtin-default";
const DEFAULT_PROFILES: SettingsProfile[] = [
  { id: BUILTIN_ID, name: "default", description: "Заводские настройки", is_builtin: true, created_at: "2026-09-01T00:00:00Z" },
];

let profiles: Map<string, SettingsProfile> = new Map(DEFAULT_PROFILES.map((p) => [p.id, p]));
/** Снапшот профиля: key → effective-значение на момент сохранения (все 97). */
let snapshots: Map<string, Record<string, unknown>> = new Map();

function currentValues(): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, s] of live) out[key] = structuredClone(s.value);
  return out;
}

function applyValues(values: Record<string, unknown>): { applied: string[]; skipped: string[] } {
  const applied: string[] = [];
  const skipped: string[] = [];
  for (const [key, v] of Object.entries(values)) {
    const s = live.get(key);
    if (!s) continue;
    if (s.envLocked) {
      skipped.push(key); // env-ключи пропускаются с отчётом
      continue;
    }
    s.value = structuredClone(v);
    s.dbValue = structuredClone(v);
    s.updatedAt = new Date().toISOString();
    s.updatedBy = "profile";
    applied.push(key);
  }
  return { applied, skipped };
}

/** Dangerous-ключи, которые профиль сдвинул относительно текущих значений. */
function dangerousMoves(values: Record<string, unknown>): string[] {
  const out: string[] = [];
  for (const [key, v] of Object.entries(values)) {
    const s = live.get(key);
    if (s && (s.meta.danger ?? false) && !valuesEqual(s.value, v)) out.push(key);
  }
  return out;
}

/* ── Обработчики контракта ── */

export async function getSettings(): Promise<SettingsPayload> {
  await delay(LATENCY_MS);
  return { settings: [...live.values()].map(toMeta), groups: GROUPS };
}

/** Серверная валидация записи: границы/enum + инварианты реестра. */
function serverValidate(s: LiveSetting, value: unknown): Record<string, string> | null {
  // validateValue ждёт полный SettingMeta — собираем его из сида
  const err = validateValue(draftMeta(s.meta, value), value);
  if (err) return { [s.meta.key]: err };
  const v = value as Record<string, unknown> | null;
  if (s.meta.t === "json" && typeof v === "object" && v !== null && !Array.isArray(v)) {
    if (s.meta.key.startsWith("schedule.")) {
      const type = v.type;
      if (type !== "interval" && type !== "crontab") {
        return { [s.meta.key]: "type должен быть interval или crontab" };
      }
    }
  }
  // Инвариант реестра §2.5: synonym < verdict <= dedup_thresholds[ns];
  // равенство verdict и dedup-порога валидно (пустая L2-зона, дефолты
  // 0.85/0.85 для dialogue_insights) → граница — минимум по namespaces
  const after = new Map(live);
  after.set(s.meta.key, { ...s, value: structuredClone(value) });
  const val = (k: string) => after.get(k)?.value as number | undefined;
  const syn = val("linker_synonym_threshold");
  const verd = val("linker_verdict_threshold");
  const dedupThresholds = after.get("dedup_thresholds")?.value as Record<string, number> | undefined;
  const dedupMin = dedupThresholds ? Math.min(...Object.values(dedupThresholds)) : undefined;
  if (syn !== undefined && verd !== undefined && syn >= verd) {
    return { [s.meta.key]: "Инвариант реестра: порог синонимии должен быть ниже порога вердикта" };
  }
  if (verd !== undefined && dedupMin !== undefined && verd > dedupMin) {
    return { [s.meta.key]: "Инвариант реестра: порог вердикта не должен превышать порог дедупликации по каждому namespace (равенство допустимо)" };
  }
  return null;
}

export async function updateSetting(key: string, value: unknown, confirm = false): Promise<SettingMeta> {
  await delay(LATENCY_MS);
  const s = live.get(key);
  if (!s) throw new ApiError(404, "Настройка не найдена");
  if (s.envLocked) {
    throw new ApiError(409, "Настройка управляется через .env / docker-compose", { keys: [key] });
  }
  const errors = serverValidate(s, value);
  if (errors) throw new ApiError(400, errors[key] ?? "Значение не прошло валидацию", { errors });
  if ((s.meta.danger ?? false) && !confirm) {
    throw new ApiError(409, "Опасное изменение — подтвердите действие", { keys: [key] });
  }
  s.value = structuredClone(value);
  s.dbValue = structuredClone(value);
  s.updatedAt = new Date().toISOString();
  s.updatedBy = "web-ui";
  return toMeta(s);
}

export async function resetSetting(key: string, confirm = false): Promise<SettingMeta> {
  await delay(LATENCY_MS);
  const s = live.get(key);
  if (!s) throw new ApiError(404, "Настройка не найдена");
  if (s.envLocked) {
    throw new ApiError(409, "Настройка управляется через .env / docker-compose", { keys: [key] });
  }
  if ((s.meta.danger ?? false) && !confirm) {
    throw new ApiError(409, "Опасный сброс — подтвердите действие", { keys: [key] });
  }
  s.value = structuredClone(s.meta.def);
  s.dbValue = null;
  s.updatedAt = new Date().toISOString();
  s.updatedBy = "web-ui";
  return toMeta(s);
}

export async function resetAllSettings(): Promise<{ reset: string[] }> {
  await delay(LATENCY_MS);
  const reset: string[] = [];
  for (const [key, s] of live) {
    if (s.envLocked) continue;
    s.value = structuredClone(s.meta.def);
    s.dbValue = null;
    s.updatedAt = new Date().toISOString();
    s.updatedBy = "web-ui";
    reset.push(key);
  }
  return { reset };
}

export async function getProfiles(): Promise<ProfilesPayload> {
  await delay(LATENCY_MS);
  return { profiles: [...profiles.values()] };
}

export async function createProfile(name: string, description?: string): Promise<SettingsProfile> {
  await delay(LATENCY_MS);
  const clean = name.trim();
  if (!clean) throw new ApiError(400, "Имя профиля не задано");
  if (clean === "default") throw new ApiError(409, "Имя default зарезервировано за builtin-профилем", { keys: [clean] });
  const profile: SettingsProfile = {
    id: `p_${Date.now().toString(36)}`,
    name: clean,
    description: description?.trim() || null,
    is_builtin: false,
    created_at: new Date().toISOString(),
  };
  profiles.set(profile.id, profile);
  snapshots.set(profile.id, currentValues());
  return profile;
}

export async function updateProfile(id: string): Promise<SettingsProfile> {
  await delay(LATENCY_MS);
  const p = profiles.get(id);
  if (!p) throw new ApiError(404, "Профиль не найден");
  if (p.is_builtin) throw new ApiError(409, "Встроенный профиль нельзя перезаписывать", { keys: [p.name] });
  snapshots.set(id, currentValues());
  return p;
}

export async function deleteProfile(id: string): Promise<{ deleted: string }> {
  await delay(LATENCY_MS);
  const p = profiles.get(id);
  if (!p) throw new ApiError(404, "Профиль не найден");
  if (p.is_builtin) throw new ApiError(409, "Встроенный профиль удалить нельзя", { keys: [p.name] });
  profiles.delete(id);
  snapshots.delete(id);
  return { deleted: id };
}

export async function applyProfile(id: string, confirm = false): Promise<ApplyProfileResult> {
  await delay(LATENCY_MS);
  const p = profiles.get(id);
  if (!p) throw new ApiError(404, "Профиль не найден");
  const values = id === BUILTIN_ID ? catalogDefaults() : snapshots.get(id);
  if (!values) throw new ApiError(409, "У профиля пустой снапшот — пересохраните его");
  const moves = dangerousMoves(values);
  if (moves.length > 0 && !confirm) {
    throw new ApiError(409, "Профиль затрагивает опасные настройки", { keys: moves });
  }
  const { applied, skipped } = applyValues(values);
  return { applied, skipped_env: skipped };
}

function catalogDefaults(): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const meta of CATALOG) out[meta.key] = structuredClone(meta.def);
  return out;
}
