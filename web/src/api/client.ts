// Thin fetch wrapper: base URL + bearer token from env, uniform errors.
// Dev runs same-origin through the Vite proxy; prod sets VITE_SELTI_API_BASE.

/** Env read lazily so tests can stub it per-case. */
function apiBase(): string {
  return import.meta.env.VITE_SELTI_API_BASE ?? "";
}

export class ApiError extends Error {
  readonly status: number;
  /** 400: детали по полям (имя ключа → текст ошибки) */
  readonly errors?: unknown;
  /** 409: массив затронутых ключей (dangerous/builtin/дубликат/env-locked) */
  readonly keys?: string[];
  constructor(status: number, message: string, extra?: { errors?: unknown; keys?: string[] }) {
    super(message);
    this.status = status;
    this.name = "ApiError";
    this.errors = extra?.errors;
    this.keys = extra?.keys;
  }
}

/** Build the absolute request URL for a path + query params. */
export function buildUrl(path: string, params?: URLSearchParams): string {
  const query = params?.toString();
  return `${apiBase()}${path}${query ? `?${query}` : ""}`;
}

function authHeaders(): Record<string, string> {
  // Bearer only when configured — localhost dev needs no token
  const token = import.meta.env.VITE_SELTI_TOKEN;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function parseError(res: Response): Promise<ApiError> {
  let message = res.statusText;
  let errors: unknown;
  let keys: string[] | undefined;
  try {
    // Живой контракт (Катерина): FastAPI оборачивает структурированные
    // ошибки в detail — {detail: {message, errors|keys}}; 404 и
    // профильные 409 несут строку в detail; прошлый контракт был плоским
    // ({message, errors, keys}) — читаем все три формы.
    const body = (await res.json()) as {
      detail?: unknown;
      message?: unknown;
      errors?: unknown;
      keys?: unknown;
    };
    if (typeof body.detail === "string") message = body.detail;
    const nested = (
      typeof body.detail === "object" && body.detail !== null ? body.detail : {}
    ) as { message?: unknown; errors?: unknown; keys?: unknown };
    if (typeof nested.message === "string") message = nested.message;
    if (typeof body.message === "string") message = body.message;
    if (nested.errors !== undefined) errors = nested.errors;
    else if (body.errors !== undefined) errors = body.errors;
    const rawKeys = nested.keys ?? body.keys;
    if (Array.isArray(rawKeys)) keys = rawKeys.map(String);
  } catch {
    // non-JSON error body — keep statusText
  }
  return new ApiError(res.status, message, { errors, keys });
}

/** GET path as JSON with typed result; non-2xx → ApiError (logged to console). */
export async function apiGet<T>(path: string, params?: URLSearchParams): Promise<T> {
  const res = await fetch(buildUrl(path, params), {
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!res.ok) {
    const err = await parseError(res);
    // Local console-only diagnostics (no remote sink by design);
    // screens surface the error themselves via state blocks
    console.error(`[api] ${res.status} ${path}: ${err.message}`);
    throw err;
  }
  return (await res.json()) as T;
}

/** Shared body-sender for POST/PUT/DELETE: JSON in, JSON out.
 * A 204/empty body resolves as undefined. */
async function apiSend<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(buildUrl(path), {
    method,
    headers: { Accept: "application/json", ...authHeaders(), ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await parseError(res);
    console.error(`[api] ${res.status} ${method} ${path}: ${err.message}`);
    throw err;
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** POST a JSON body; non-2xx → ApiError. */
export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiSend<T>("POST", path, body);
}

/** PUT a JSON body; non-2xx → ApiError. */
export function apiPut<T>(path: string, body?: unknown): Promise<T> {
  return apiSend<T>("PUT", path, body);
}

/** DELETE without a body; non-2xx → ApiError. */
export function apiDelete<T>(path: string): Promise<T> {
  return apiSend<T>("DELETE", path);
}
