import type { RemoteScan } from './api';
import type { LocalScan, UploadProgress } from '../../modules/scanner-capture';

export type ScanRow = {
  key: string;
  localId?: string;
  serverId?: string;
  name: string;
  createdAt?: string;
  thumbnailUri?: string;
  /** Recorded | Uploading 42% | Queued | Processing · sfm | Ready | Failed */
  statusLabel: string;
  status: 'recording' | 'recorded' | 'uploading' | 'queued' | 'processing' | 'ready' | 'failed' | 'created';
  progress?: number;
  viewerUrl?: string | null;
  sizeBytes?: number;
};

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export function uploadFraction(p?: Pick<UploadProgress, 'sentBytes' | 'totalBytes'> | null): number | undefined {
  if (!p || !p.totalBytes) return undefined;
  return Math.max(0, Math.min(1, p.sentBytes / p.totalBytes));
}

/** Merges local packages (not yet completed) with server scans. Local wins until upload completes. */
export function mergeScans(
  local: LocalScan[],
  remote: RemoteScan[],
  progress: Record<string, UploadProgress | undefined> = {},
): ScanRow[] {
  const rows: ScanRow[] = [];
  const claimed = new Set<string>();
  for (const l of local) {
    const serverId = l.meta.serverScanId;
    const r = serverId ? remote.find((x) => x.scan_id === serverId) : remote.find((x) => x.scan_id === l.scanId);
    if (r) claimed.add(r.scan_id);
    const phase = l.meta.phase ?? 'recorded';
    let status: ScanRow['status'] = 'recorded';
    let statusLabel = 'Recorded';
    let frac: number | undefined;
    if (phase === 'recording') {
      status = 'recording';
      statusLabel = 'Recording…';
    } else if (phase === 'failed') {
      status = 'failed';
      statusLabel = 'Upload failed';
    } else if (phase === 'creating' || phase === 'uploading') {
      status = 'uploading';
      frac = uploadFraction(progress[l.scanId]);
      statusLabel = frac === undefined ? 'Uploading' : `Uploading ${Math.floor(frac * 100)}%`;
    } else if (phase === 'completing') {
      status = 'uploading';
      frac = 1;
      statusLabel = 'Finishing upload';
    }
    rows.push({
      key: `local:${l.scanId}`,
      localId: l.scanId,
      serverId,
      name: l.name ?? r?.name ?? l.scanId,
      createdAt: l.createdAt ?? r?.created_at,
      thumbnailUri: l.thumbnailUri ?? r?.thumbnail_url ?? undefined,
      status,
      statusLabel,
      progress: frac,
      viewerUrl: r?.viewer_url,
      sizeBytes: l.sizeBytes,
    });
  }
  for (const r of remote) {
    if (claimed.has(r.scan_id)) continue;
    rows.push({
      key: `remote:${r.scan_id}`,
      serverId: r.scan_id,
      name: r.name || r.scan_id,
      createdAt: r.created_at,
      thumbnailUri: r.thumbnail_url ?? undefined,
      status: r.status,
      statusLabel: remoteLabel(r),
      progress: r.progress ?? undefined,
      viewerUrl: r.viewer_url,
    });
  }
  return rows.sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''));
}

export function remoteLabel(r: Pick<RemoteScan, 'status' | 'stage' | 'progress'>): string {
  switch (r.status) {
    case 'processing': {
      const pct = typeof r.progress === 'number' ? ` ${Math.round(r.progress * 100)}%` : '';
      return `Processing${r.stage ? ` · ${r.stage}` : ''}${pct}`;
    }
    case 'created':
      return 'Created';
    default:
      return cap(r.status);
  }
}

export function formatBytes(n?: number): string {
  if (n === undefined || n === null || !isFinite(n)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1000 && i < u.length - 1) {
    v /= 1000;
    i++;
  }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`;
}

export function formatDuration(s?: number): string {
  if (s === undefined || !isFinite(s)) return '—';
  const t = Math.floor(s);
  const m = Math.floor(t / 60);
  const sec = t % 60;
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

/** Rough upload-size estimate while recording (R3.10): bytes so far is already the package size. */
export function estimateUploadBytes(bytesWritten: number, elapsedS: number, remainingS = 0): number {
  if (elapsedS <= 0) return bytesWritten;
  return bytesWritten + (bytesWritten / elapsedS) * remainingS;
}
