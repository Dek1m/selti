// Thin fetch wrapper: base URL + bearer token from env, uniform errors.
// Dev runs same-origin through the Vite proxy; prod sets VITE_SELTI_API_BASE.

/** Env read lazily so tests can stub it per-case. */
function apiBase(): string {
  return import.meta.env.VITE_SELTI_API_BASE ?? "";
}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
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
  let detail = res.statusText;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // non-JSON error body — keep statusText
  }
  return new ApiError(res.status, detail);
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
