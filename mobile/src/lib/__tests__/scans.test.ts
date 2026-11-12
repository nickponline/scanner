import type { LocalScanMeta } from '../../../modules/scanner-capture';
import { estimateUploadBytes, formatBytes, formatDuration, mergeScans } from '../scans';

const local = (id: string, meta: LocalScanMeta, createdAt = '2026-11-07T10:00:00Z') => ({
  scanId: id,
  dir: '',
  sizeBytes: 5e9,
  hasManifest: true,
  name: `L ${id}`,
  createdAt,
  meta,
});

test('mergeScans: local uploading row shows %, claims its server row; remote-only rows appended', () => {
  const rows = mergeScans(
    [local('scan_a', { phase: 'uploading', serverScanId: 'srv_a' })],
    [
      { scan_id: 'srv_a', name: 'A', created_at: '2026-11-07T10:00:00Z', status: 'uploading', viewer_url: null },
      {
        scan_id: 'srv_b',
        name: 'B',
        created_at: '2026-11-06T10:00:00Z',
        status: 'processing',
        stage: 'sfm',
        progress: 0.25,
        viewer_url: 'https://v/b',
      },
    ],
    { scan_a: { scanId: 'scan_a', sentBytes: 42, totalBytes: 100, filesDone: 1, filesTotal: 3 } },
  );
  expect(rows).toHaveLength(2);
  expect(rows[0]).toMatchObject({ localId: 'scan_a', serverId: 'srv_a', statusLabel: 'Uploading 42%', status: 'uploading' });
  expect(rows[1]).toMatchObject({ serverId: 'srv_b', statusLabel: 'Processing · sfm 25%', viewerUrl: 'https://v/b' });
});

test('mergeScans: recorded/failed labels', () => {
  const rows = mergeScans([local('scan_a', {}), local('scan_b', { phase: 'failed' }, '2026-11-08T00:00:00Z')], []);
  expect(rows.map((r) => r.statusLabel)).toEqual(['Upload failed', 'Recorded']);
});

test('formatters', () => {
  expect(formatBytes(0)).toBe('0 B');
  expect(formatBytes(1_500_000)).toBe('1.5 MB');
  expect(formatBytes(12_300_000_000)).toBe('12.3 GB');
  expect(formatDuration(125.9)).toBe('2:05');
  expect(estimateUploadBytes(100, 10, 10)).toBe(200);
});
