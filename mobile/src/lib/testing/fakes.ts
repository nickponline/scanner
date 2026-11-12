import type { LocalScan, LocalScanMeta, UploadState, UploadTarget } from '../../../modules/scanner-capture';
import { ApiError } from '../api';
import type { NativeUploads } from '../orchestrator';

export const MANIFEST = {
  schema: 'capture_manifest.v1',
  scan_id: 'scan_01TEST',
  name: 'Flat',
  files: [
    { path: 'video/seg_000.mov', size: 100, crc32c: 'AAAAAA==' },
    { path: 'frames/seg_000.jsonl', size: 10, crc32c: 'AAAAAA==' },
  ],
};

/** In-memory stand-in for the Swift module (files + meta.json + UploadManager). */
export class FakeNative implements NativeUploads {
  scans = new Map<string, { meta: LocalScanMeta; manifest: string | null }>();
  jobs = new Map<string, UploadState>();
  calls: string[] = [];

  addScan(id = 'scan_01TEST', meta: LocalScanMeta = { phase: 'recorded' }) {
    this.scans.set(id, { meta, manifest: JSON.stringify({ ...MANIFEST, scan_id: id }) });
  }
  async listLocalScans(): Promise<LocalScan[]> {
    return [...this.scans.keys()].map((id) => this.describe(id)!);
  }
  describe(id: string): LocalScan | null {
    const s = this.scans.get(id);
    if (!s) return null;
    return { scanId: id, dir: `/docs/scans/${id}`, sizeBytes: 110, hasManifest: !!s.manifest, meta: s.meta };
  }
  async getLocalScan(id: string) {
    return this.describe(id);
  }
  async readManifest(id: string) {
    return this.scans.get(id)?.manifest ?? null;
  }
  async setScanMeta(id: string, meta: LocalScanMeta) {
    const s = this.scans.get(id);
    if (s) s.meta = meta;
  }
  async deleteLocalScan(id: string) {
    this.calls.push(`delete:${id}`);
    this.scans.delete(id);
  }
  async startUpload(id: string, uploads: UploadTarget[], wifiOnly: boolean) {
    this.calls.push(`start:${id}:${uploads.map((u) => u.path).join(',')}:wifi=${wifiOnly}`);
    const prev = this.jobs.get(id);
    this.jobs.set(id, {
      scanId: id,
      state: 'uploading',
      wifiOnly,
      sentBytes: 0,
      totalBytes: 110,
      filesDone: 0,
      filesTotal: uploads.length,
      files: uploads.map((u) => {
        const old = prev?.files.find((f) => f.path === u.path);
        return old?.status === 'done' ? old : { path: u.path, size: 1, committed: 0, status: 'pending' as const };
      }),
      expiredPaths: [],
    });
  }
  async updateUploadUrls(id: string, uploads: UploadTarget[]) {
    this.calls.push(`update:${id}:${uploads.map((u) => u.path).join(',')}`);
    const j = this.jobs.get(id)!;
    j.state = 'uploading';
    j.expiredPaths = [];
    for (const f of j.files) if (uploads.some((u) => u.path === f.path)) f.status = 'pending';
  }
  async getUploadState(id: string) {
    return this.jobs.get(id) ?? null;
  }
  async forgetUpload(id: string) {
    this.calls.push(`forget:${id}`);
    this.jobs.delete(id);
  }
  /** Simulate the background session finishing / expiring. */
  finish(id: string) {
    const j = this.jobs.get(id)!;
    j.state = 'done';
    j.files.forEach((f) => (f.status = 'done'));
    return { ...j };
  }
  expire(id: string, paths: string[]) {
    const j = this.jobs.get(id)!;
    j.state = 'needsUrls';
    j.expiredPaths = paths;
    j.files.forEach((f) => paths.includes(f.path) && (f.status = 'expired'));
    return { ...j };
  }
}

export class FakeApi {
  calls: string[] = [];
  completeErrors: ApiError[] = [];
  createError?: Error;
  async createScan(manifest: { scan_id: string }) {
    this.calls.push(`create:${manifest.scan_id}`);
    if (this.createError) throw this.createError;
    return {
      scan_id: 'srv_1',
      uploads: ['manifest.json', ...MANIFEST.files.map((f) => f.path)].map((path) => ({
        path,
        url: `https://storage.googleapis.com/upload?upload_id=${encodeURIComponent(path)}`,
      })),
    };
  }
  async refreshUploads(scanId: string, paths: string[]) {
    this.calls.push(`refresh:${scanId}:${paths.join(',')}`);
    return { uploads: paths.map((path) => ({ path, url: `https://fresh/${path}` })) };
  }
  async complete(scanId: string) {
    this.calls.push(`complete:${scanId}`);
    const e = this.completeErrors.shift();
    if (e) throw e;
    return { status: 'queued' as const };
  }
}
