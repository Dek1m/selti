// TypeScript mirrors of the selti REST contracts (memory_server/api/web.py).
// Field order follows the backend models; new backend fields extend these
// interfaces at the end.

export type GranuleStatus = "asserted" | "superseded" | "retracted" | "uncertain";

/** GET /api/search item — SearchResult model */
export interface SearchHit {
  id: string;
  content: string;
  metadata: Record<string, unknown>;
  importance: number;
  score: number;
  project_id: string | null;
  status: GranuleStatus;
  namespace: string | null;
  created_at: string | null;
  last_accessed_at: string | null;
  frozen: boolean;
  score_rrf: number | null;
  score_decay: number | null;
  score_importance: number | null;
  /** map_layout coordinates (?with_positions=1; no row — field absent) */
  position?: [number, number, number];
}

/** GET /api/memories/{id} — MemoryRecord model */
export interface MemoryRecord {
  id: string;
  user_id: string;
  content: string;
  metadata: Record<string, unknown>;
  namespace: string;
  importance: number;
  created_at: string;
  updated_at: string;
  content_hash: string | null;
  project_id: string | null;
  status: GranuleStatus;
  valid_from: string | null;
  valid_to: string | null;
  ingested_at: string | null;
  confidence: number;
  supersedes: string | null;
  superseded_by: string | null;
  frozen: boolean;
  last_accessed_at: string | null;
  access_count: number;
  /** map_layout coordinates (?with_positions=1; no row — field absent) */
  position?: [number, number, number];
}

/** GET /api/memories/{id}?include_history=true adds this field */
export interface MemoryDetail extends MemoryRecord {
  history?: HistoryPayload;
}

/** MemoryHistory model — oldest → newest; current_id = asserted version */
export interface HistoryPayload {
  items: MemoryRecord[];
  current_id: string | null;
}

/** Relation model */
export interface Relation {
  id: string;
  source_id: string;
  target_id: string | null;
  target_name: string | null;
  link_type: string;
  description: string | null;
  weight: number;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

/** GET /api/memories/{id}/relations */
export interface RelationsPayload {
  incoming: Relation[];
  outgoing: Relation[];
  /** neighbor map_layout coordinates (?with_positions=1): {id: [x, y, z]} */
  positions?: Record<string, [number, number, number]>;
}

/** GET /api/namespaces item — registry entry for the spectrum */
export interface NamespaceInfo {
  uid: string;
  name: string;
  description: string | null;
}

/** GET /api/stats item — MemoryStatsItem model */
export interface NamespaceStat {
  namespace: string;
  count: number;
  last_updated: string | null;
}

/** GET /api/projects item (registry card, no stack) — _PROJECT_COLUMNS */
export interface ProjectCard {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  kind: string;
  status: string;
  local_path: string | null;
  repo_url: string | null;
}

export interface ProjectLink {
  link_type: string;
  url: string;
  title: string | null;
}

export interface ProjectTechnology {
  name: string;
  category: string | null;
  docs_url: string | null;
  version: string | null;
  purpose: string | null;
}

/** GET /api/projects/{slug} — full card with stack */
export interface ProjectDetail extends ProjectCard {
  docs_url: string | null;
  homepage_url: string | null;
  updated_at: string | null;
  links: ProjectLink[];
  technologies: ProjectTechnology[];
}

/** GET /api/contexts/{slug} — ProjectContext model («облачко знаний»)
 * sections: {stack, decisions, code, insights, infra, …} — line arrays */
export interface ProjectContext {
  project_id: string;
  content: string | null;
  sections: Record<string, string[]>;
  granule_count: number;
  computed_at: string | null;
  stale: boolean;
}

/** GET /health — readiness payload */
export interface HealthPayload {
  status: "ok" | "degraded";
  server: string;
  version: string;
  checks: {
    config?: {
      dedup_enabled: boolean;
      api_key_configured: boolean;
      redis_configured: boolean;
    };
    postgres?: string;
    redis?: string;
    celery?: string;
  };
}

/* ── Settings (Ф3 «Конфигурация», контракт Соны, реестр SETTINGS_REGISTRY.md) ── */

export type SettingValueType = "int" | "float" | "bool" | "str" | "json";

/** Where the effective value comes from: env beats DB, DB beats default. */
export type EffectiveSource = "env" | "db" | "default";

/** Виджет из реестра (сидинг 027); фронт мапит на контролы,
 * неизвестные значения фолбэкасят по value_type. */
export type SettingWidget =
  | "switch"
  | "slider_number"
  | "number"
  | "combobox"
  | "checkboxes"
  | "kv_table"
  | "text";

/** GET /api/settings item — SettingMeta model */
export interface SettingMeta {
  key: string;
  /** Эффективное значение (env > db > default) */
  value: unknown;
  /** Сырое значение из БД app_settings; null — в БД не записано */
  db_value: unknown;
  value_type: SettingValueType;
  /** group_key из миграции 027 (search, dedup, …, api_caps) */
  group: string;
  title_ru: string;
  description_ru: string;
  default_value: unknown;
  min_value?: number;
  max_value?: number;
  /** Варианты для combobox / checkboxes */
  enum_values?: string[];
  is_dangerous: boolean;
  requires_restart: boolean;
  /** Жёсткая блокировка поля UI: ключ задан в env/compose */
  is_env_locked: boolean;
  effective_source: EffectiveSource;
  differs_from_default: boolean;
  /** Желаемый виджет из реестра */
  widget: SettingWidget;
  updated_at: string | null;
  updated_by: string | null;
}

/** GET /api/settings — payload: каталог + русские заголовки групп из сидинга */
export interface SettingsGroupInfo {
  key: string;
  title_ru: string;
}

export interface SettingsPayload {
  settings: SettingMeta[];
  groups: SettingsGroupInfo[];
}

/** GET /api/settings/profiles item */
export interface SettingsProfile {
  id: string;
  name: string;
  description?: string | null;
  is_builtin: boolean;
  created_at: string;
}

/** GET /api/settings/profiles — payload */
export interface ProfilesPayload {
  profiles: SettingsProfile[];
}

/** POST /api/settings/profiles/{id}/apply — env-ключи внутри профиля пропускаются */
export interface ApplyProfileResult {
  applied: string[];
  skipped_env: string[];
}
