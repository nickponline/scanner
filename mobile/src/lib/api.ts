/** Client for the Scanner HTTP API (docs/CONTRACTS.md §3). `fetch` is injectable for tests. */

export type ScanStatus = 'created' | 'uploading' | 'queued' | 'processing' | 'ready' | 'failed';
export type Stage = 'ingest' | 'frames' | 'sfm' | 'depth' | 'splat' | 'lod' | 'mesh' | 'plan' | 'publish';

export type RemoteScan = {
  scan_id: string;
  name: string;
  created_at: string;
  status: ScanStatus;
  stage?: Stage | string | null;
  progress?: number | null;
  thumbnail_url?: string | null;
  viewer_url?: string | null;
};

export type StageInfo = {
  status: string;
  started_at?: string | null;
  ended_at?: string | null;
  metrics?: Record<string, unknown> | null;
  error?: string | null;
};

export type RemoteScanDetail = RemoteScan & {
  stages?: Record<string, StageInfo>;
  versions?: unknown[];
  error?: string | null;
};

export type UploadTarget = { path: string; url: string };
export type CreateScanResponse = { scan_id: string; uploads: UploadTarget[] };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
  /** Network failure (status 0) or server-side / throttling: worth retrying later. */
  get retryable(): boolean {
    return this.status === 0 || this.status === 408 || this.status === 429 || this.status >= 500;
  }
}

export type ApiConfig = { baseUrl: string; token: string };
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** Accepts `https://host`, `https://host/` or `https://host/v1` and returns `https://host/v1`. */
export function normalizeBaseUrl(raw: string): string {
  let u = raw.trim().replace(/\/+$/, '');
  if (!u) return '';
  if (!/^https?:\/\//i.test(u)) u = `https://${u}`;
  if (!/\/v1$/.test(u)) u = `${u}/v1`;
  return u;
}

export class ApiClient {
  constructor(
    private readonly getConfig: () => ApiConfig | Promise<ApiConfig>,
    private readonly fetchImpl: FetchLike = (i, init) => fetch(i, init),
  ) {}

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const cfg = await this.getConfig();
    const base = normalizeBaseUrl(cfg.baseUrl);
    if (!base) throw new ApiError('API base URL is not set (Settings)', -1);
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let res: Response;
    try {
      res = await this.fetchImpl(`${base}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      throw new ApiError(`Network error: ${(e as Error)?.message ?? e}`, 0);
    }
    const text = await res.text();
    let json: unknown = undefined;
    if (text) {
      try {
        json = JSON.parse(text);
      } catch {
        json = text;
      }
    }
    if (!res.ok) {
      const detail =
        (json && typeof json === 'object' && ('detail' in json || 'error' in json)
          ? JSON.stringify((json as Record<string, unknown>).detail ?? (json as Record<string, unknown>).error)
          : typeof json === 'string'
            ? json.slice(0, 200)
            : '') || res.statusText;
      throw new ApiError(`${method} ${path} → ${res.status} ${detail}`, res.status, json);
    }
    return json as T;
  }

  createScan(manifest: unknown) {
    return this.request<CreateScanResponse>('POST', '/scans', { manifest });
  }
  refreshUploads(scanId: string, paths: string[]) {
    return this.request<{ uploads: UploadTarget[] }>('POST', `/scans/${encodeURIComponent(scanId)}/uploads`, { paths });
  }
  complete(scanId: string) {
    return this.request<{ status: ScanStatus }>('POST', `/scans/${encodeURIComponent(scanId)}/complete`);
  }
  listScans() {
    return this.request<RemoteScan[]>('GET', '/scans');
  }
  getScan(scanId: string) {
    return this.request<RemoteScanDetail>('GET', `/scans/${encodeURIComponent(scanId)}`);
  }
  registerPushToken(expoPushToken: string) {
    return this.request<void>('POST', '/devices/push-token', { expo_push_token: expoPushToken });
  }
}
