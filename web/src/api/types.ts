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

/** GET /api/projects item (registry card, no stack) */
export interface ProjectCard {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  kind: string;
  status: string;
  updated_at: string | null;
}
