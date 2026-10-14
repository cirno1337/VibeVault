const KEY = "vibevault.token";

export function getToken(): string | null {
  try { return sessionStorage.getItem(KEY); } catch { return null; }
}
export function setToken(t: string | null): void {
  try { t ? sessionStorage.setItem(KEY, t) : sessionStorage.removeItem(KEY); } catch { /* storage unavailable */ }
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const token = getToken();
  const res = await fetch(`/api/v1${path}`, {
    method: init.method ?? "GET",
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(init.body ? { "Content-Type": "application/json" } : {}) },
    body: init.body ? JSON.stringify(init.body) : undefined,
  });
  if (!res.ok) {
    let code = "http_error", message = res.statusText;
    try { const j = await res.json(); code = j.error?.code ?? code; message = j.error?.message ?? message; } catch { /* non-JSON error */ }
    if (res.status === 401) window.dispatchEvent(new Event("vibevault:unauthorized"));
    throw new ApiError(res.status, code, message);
  }
  return res.json() as Promise<T>;
}
