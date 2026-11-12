// Where the scene comes from (fixture vs API), auth, and URL resolution.

import type { SceneJson } from "./types";

const TOKEN_KEY = "scanner.token";

export type Source =
  | { mode: "fixture"; sceneUrl: string }
  | { mode: "api"; scanId: string; version?: string; share?: string; apiBase: string }
  | { mode: "list"; apiBase: string }
  | { mode: "report"; scanId: string; version?: string; apiBase: string };

export function parseSource(loc: Location = window.location): Source {
  const q = new URLSearchParams(loc.search);
  const scene = q.get("scene");
  if (scene) return { mode: "fixture", sceneUrl: new URL(scene, loc.href).href };
  const apiBase = (q.get("api") || loc.origin).replace(/\/+$/, "");
  const rm = loc.pathname.match(/\/scans\/([^/]+)\/report\/?$/);
  if (rm) return { mode: "report", scanId: decodeURIComponent(rm[1]), version: q.get("v") ?? undefined, apiBase };
  const m = loc.pathname.match(/\/scans\/([^/]+)\/?$/);
  if (m) {
    return {
      mode: "api",
      scanId: decodeURIComponent(m[1]),
      version: q.get("v") ?? undefined,
      share: q.get("share") ?? undefined,
      apiBase,
    };
  }
  return { mode: "list", apiBase };
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(t: string | null) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
}

export class AuthError extends Error {}

/** Auth context: adds a bearer header only for requests to the API origin (never to signed GCS URLs). */
export class Fetcher {
  constructor(
    public apiBase: string | null,
    public share?: string,
  ) {}

  headersFor(url: string): Record<string, string> {
    if (!this.apiBase || this.share) return {};
    const tok = getToken();
    if (!tok) return {};
    try {
      // Only /v1/scans* needs the bearer token; /v1/f/<token>/… asset URLs and signed URLs need no headers.
      const u = new URL(url);
      if (u.origin === new URL(this.apiBase).origin && u.pathname.startsWith("/v1/scans")) return { Authorization: `Bearer ${tok}` };
    } catch {
      /* relative/invalid */
    }
    return {};
  }

  async fetch(url: string, init: RequestInit = {}): Promise<Response> {
    const res = await fetch(url, { ...init, headers: { ...this.headersFor(url), ...(init.headers || {}) } });
    if (res.status === 401 || res.status === 403) throw new AuthError(`${res.status} for ${url}`);
    if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
    return res;
  }

  async json<T>(url: string): Promise<T> {
    return (await this.fetch(url)).json() as Promise<T>;
  }

  async text(url: string): Promise<string> {
    return (await this.fetch(url)).text();
  }

  /** URL usable in <img src>; goes through fetch+blob when an auth header is needed. */
  async imageUrl(url: string): Promise<string> {
    if (Object.keys(this.headersFor(url)).length === 0) return url;
    const blob = await (await this.fetch(url)).blob();
    return URL.createObjectURL(blob);
  }
}

export function resolveUrl(rel: string, base: string): string {
  return new URL(rel, base).href;
}

export interface LoadedScene {
  scene: SceneJson;
  /** Base for resolving relative URLs in scene.json. */
  base: string;
  fetcher: Fetcher;
}

export async function loadScene(src: Source): Promise<LoadedScene> {
  if (src.mode === "fixture") {
    const fetcher = new Fetcher(null);
    const scene = await fetcher.json<SceneJson>(src.sceneUrl);
    return { scene, base: scene.base_url ? resolveUrl(scene.base_url, src.sceneUrl) : src.sceneUrl, fetcher };
  }
  if (src.mode !== "api") throw new Error("no scene");
  const fetcher = new Fetcher(src.apiBase, src.share);
  const q = new URLSearchParams();
  if (src.version) q.set("v", String(parseInt(src.version, 10)));
  if (src.share) q.set("share", src.share);
  const qs = q.toString();
  const url = `${src.apiBase}/v1/scans/${encodeURIComponent(src.scanId)}/scene${qs ? `?${qs}` : ""}`;
  const scene = await fetcher.json<SceneJson>(url);
  return { scene, base: scene.base_url ? resolveUrl(scene.base_url, url) : url, fetcher };
}

export interface ScanSummary {
  scan_id: string;
  name: string;
  created_at: string;
  status: string;
  stage?: string;
  progress?: number;
  thumbnail_url?: string;
  viewer_url?: string;
  stage_states?: Record<string, string>;
  progress_note?: string;
  failed_stage?: string;
  partial?: boolean;
  processing_version?: number;
  current_version?: number;
}

export async function listScans(apiBase: string): Promise<ScanSummary[]> {
  return new Fetcher(apiBase).json<ScanSummary[]>(`${apiBase}/v1/scans`);
}
