/**
 * Upload / resume orchestration (pure TS, no React Native imports — unit tested with jest).
 *
 *  recorded ─POST /scans─► uploading ─native background upload─► completing ─POST /complete─► completed
 *                              ▲   │ needsUrls: POST /scans/{id}/uploads → updateUploadUrls
 *                              └───┘
 * Local files are deleted only after /complete succeeds (R3.14). Every transition is persisted in the
 * scan's native meta.json, so `resumeAll()` can pick up after an app kill at any point.
 */
import { ApiError, type ApiClient } from './api';
import type { LocalPhase, LocalScan, LocalScanMeta, UploadState, UploadTarget } from '../../modules/scanner-capture';

export interface NativeUploads {
  listLocalScans(): Promise<LocalScan[]>;
  getLocalScan(scanId: string): Promise<LocalScan | null>;
  readManifest(scanId: string): Promise<string | null>;
  setScanMeta(scanId: string, meta: LocalScanMeta): Promise<void>;
  deleteLocalScan(scanId: string): Promise<void>;
  startUpload(scanId: string, uploads: UploadTarget[], wifiOnly: boolean): Promise<void>;
  updateUploadUrls(scanId: string, uploads: UploadTarget[]): Promise<void>;
  getUploadState(scanId: string): Promise<UploadState | null>;
  forgetUpload(scanId: string): Promise<void>;
}

export type OrchestratorDeps = {
  api: Pick<ApiClient, 'createScan' | 'refreshUploads' | 'complete'>;
  native: NativeUploads;
  getWifiOnly: () => boolean | Promise<boolean>;
  now?: () => Date;
  log?: (msg: string) => void;
};

export type ProcessResult = { scanId: string; phase: LocalPhase; error?: string };

const ACTIVE: LocalPhase[] = ['recorded', 'creating', 'uploading', 'completing'];

export class UploadOrchestrator {
  private inflight = new Map<string, Promise<ProcessResult>>();
  private listeners = new Set<(r: ProcessResult) => void>();

  constructor(private readonly d: OrchestratorDeps) {}

  onChange(fn: (r: ProcessResult) => void) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Advance one scan as far as it can go right now. Concurrent calls for the same scan share a run. */
  process(scanId: string): Promise<ProcessResult> {
    const running = this.inflight.get(scanId);
    if (running) return running;
    const p = this.run(scanId).finally(() => this.inflight.delete(scanId));
    this.inflight.set(scanId, p);
    return p;
  }

  /** On launch / foreground / network change: advance every unfinished local scan. */
  async resumeAll(): Promise<ProcessResult[]> {
    const scans = await this.d.native.listLocalScans();
    const todo = scans.filter((s) => s.hasManifest && ACTIVE.includes((s.meta.phase ?? 'recorded') as LocalPhase));
    const out: ProcessResult[] = [];
    for (const s of todo) out.push(await this.process(s.scanId));
    return out;
  }

  /** Native `onUploadState` event. */
  handleUploadState(state: UploadState): Promise<ProcessResult> | undefined {
    if (state.state === 'done' || state.state === 'needsUrls' || state.state === 'error') {
      return this.process(state.scanId);
    }
    return undefined;
  }

  /** User tapped "Retry" on a failed scan. */
  async retry(scanId: string): Promise<ProcessResult> {
    const scan = await this.d.native.getLocalScan(scanId);
    if (!scan) return { scanId, phase: 'completed' };
    const phase: LocalPhase = scan.meta.serverScanId ? 'uploading' : 'recorded';
    await this.setMeta(scanId, scan.meta, { phase, error: undefined, retryAll: true });
    return this.process(scanId);
  }

  // ---------------------------------------------------------------------------------------------

  private async run(scanId: string): Promise<ProcessResult> {
    const scan = await this.d.native.getLocalScan(scanId);
    if (!scan) return { scanId, phase: 'completed' };
    let meta: LocalScanMeta = { ...scan.meta };
    try {
      // Bounded loop: each iteration performs one transition.
      for (let step = 0; step < 6; step++) {
        const phase = (meta.phase ?? 'recorded') as LocalPhase;
        switch (phase) {
          case 'recording':
          case 'completed':
          case 'failed':
            return this.emit({ scanId, phase, error: meta.error });
          case 'recorded':
          case 'creating':
            meta = await this.create(scanId, meta);
            break;
          case 'uploading': {
            const next = await this.checkUpload(scanId, meta);
            if (next === meta) return this.emit({ scanId, phase: 'uploading' });
            meta = next;
            break;
          }
          case 'completing':
            meta = await this.complete(scanId, meta);
            if (meta.phase === 'completed') return this.emit({ scanId, phase: 'completed' });
            break;
        }
      }
      return this.emit({ scanId, phase: meta.phase as LocalPhase, error: meta.error });
    } catch (e) {
      const err = e as Error;
      const retryable = e instanceof ApiError ? e.retryable : false;
      this.d.log?.(`[upload] ${scanId}: ${err.message}`);
      if (retryable) {
        // Keep the phase; resumeAll() / next network change retries.
        await this.setMeta(scanId, meta, { error: err.message });
        return this.emit({ scanId, phase: meta.phase as LocalPhase, error: err.message });
      }
      meta = await this.setMeta(scanId, meta, { phase: 'failed', error: err.message, failedPhase: meta.phase });
      return this.emit({ scanId, phase: 'failed', error: err.message });
    }
  }

  private async create(scanId: string, meta: LocalScanMeta): Promise<LocalScanMeta> {
    const raw = await this.d.native.readManifest(scanId);
    if (!raw) throw new Error('manifest.json missing');
    const manifest = JSON.parse(raw);
    meta = await this.setMeta(scanId, meta, { phase: 'creating' });
    const res = await this.d.api.createScan(manifest);
    if (!res?.scan_id || !Array.isArray(res.uploads)) throw new Error('Bad POST /scans response');
    meta = await this.setMeta(scanId, meta, {
      phase: 'uploading',
      serverScanId: res.scan_id,
      uploadPaths: res.uploads.map((u) => u.path),
      error: undefined,
    });
    await this.d.native.startUpload(scanId, res.uploads, await this.d.getWifiOnly());
    return meta;
  }

  /** Returns the same object when nothing changed (upload still running in the background). */
  private async checkUpload(scanId: string, meta: LocalScanMeta): Promise<LocalScanMeta> {
    const serverId = meta.serverScanId;
    if (!serverId) return this.setMeta(scanId, meta, { phase: 'recorded' });
    const st = await this.d.native.getUploadState(scanId);
    if (!st || meta.retryAll) {
      // Native job lost (or a user retry): get fresh session URIs for everything and restart.
      // Fresh sessions start at offset 0; native keeps progress for files already done.
      const paths = (meta.uploadPaths as string[] | undefined) ?? (await this.manifestPaths(scanId));
      const pending = st ? st.files.filter((f) => f.status !== 'done').map((f) => f.path) : paths;
      if (pending.length) {
        const { uploads } = await this.d.api.refreshUploads(serverId, pending);
        await this.d.native.startUpload(scanId, uploads, await this.d.getWifiOnly());
      }
      meta = await this.setMeta(scanId, meta, { retryAll: undefined, error: undefined });
      return pending.length ? meta : this.setMeta(scanId, meta, { phase: 'completing' });
    }
    switch (st.state) {
      case 'done':
        return this.setMeta(scanId, meta, { phase: 'completing', error: undefined });
      case 'needsUrls': {
        const { uploads } = await this.d.api.refreshUploads(serverId, st.expiredPaths);
        await this.d.native.updateUploadUrls(scanId, uploads);
        return meta;
      }
      case 'error':
        throw new Error(st.error ?? 'upload failed');
      default:
        return meta;
    }
  }

  private async complete(scanId: string, meta: LocalScanMeta): Promise<LocalScanMeta> {
    const serverId = meta.serverScanId;
    if (!serverId) return this.setMeta(scanId, meta, { phase: 'recorded' });
    try {
      await this.d.api.complete(serverId);
    } catch (e) {
      // 409/400: server says files/checksums don't match → re-upload everything that's wrong.
      if (e instanceof ApiError && (e.status === 409 || e.status === 400 || e.status === 412)) {
        return this.setMeta(scanId, meta, { phase: 'uploading', retryAll: true, error: e.message });
      }
      throw e;
    }
    // Server verified size + CRC32C of every object: now (and only now) delete local copies.
    await this.d.native.forgetUpload(scanId);
    await this.d.native.deleteLocalScan(scanId);
    return { ...meta, phase: 'completed' };
  }

  private async manifestPaths(scanId: string): Promise<string[]> {
    const raw = await this.d.native.readManifest(scanId);
    if (!raw) return [];
    const m = JSON.parse(raw) as { files?: { path: string }[] };
    return ['manifest.json', ...(m.files ?? []).map((f) => f.path)];
  }

  private async setMeta(scanId: string, meta: LocalScanMeta, patch: Record<string, unknown>): Promise<LocalScanMeta> {
    const next: LocalScanMeta = { ...meta, ...patch, updatedAt: (this.d.now?.() ?? new Date()).toISOString() };
    for (const k of Object.keys(next)) if (next[k] === undefined) delete next[k];
    await this.d.native.setScanMeta(scanId, next);
    return next;
  }

  private emit(r: ProcessResult): ProcessResult {
    this.listeners.forEach((fn) => fn(r));
    return r;
  }
}
