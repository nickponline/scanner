import { ApiError } from '../api';
import { UploadOrchestrator } from '../orchestrator';
import { FakeApi, FakeNative } from '../testing/fakes';

function setup(meta?: Parameters<FakeNative['addScan']>[1]) {
  const native = new FakeNative();
  const api = new FakeApi();
  native.addScan('scan_01TEST', meta);
  const orch = new UploadOrchestrator({ api, native, getWifiOnly: () => true, now: () => new Date(0) });
  return { native, api, orch };
}

describe('UploadOrchestrator', () => {
  test('happy path: create → upload → complete → delete local files', async () => {
    const { native, api, orch } = setup();
    const r1 = await orch.process('scan_01TEST');
    expect(r1.phase).toBe('uploading');
    expect(api.calls).toEqual(['create:scan_01TEST']);
    expect(native.calls[0]).toBe('start:scan_01TEST:manifest.json,video/seg_000.mov,frames/seg_000.jsonl:wifi=true');
    expect(native.scans.get('scan_01TEST')!.meta).toMatchObject({ phase: 'uploading', serverScanId: 'srv_1' });

    // Background session reports done via the native onUploadState event.
    const r2 = await orch.handleUploadState(native.finish('scan_01TEST'))!;
    expect(r2.phase).toBe('completed');
    expect(api.calls).toEqual(['create:scan_01TEST', 'complete:srv_1']);
    expect(native.calls).toContain('forget:scan_01TEST');
    expect(native.calls).toContain('delete:scan_01TEST');
    expect(native.scans.has('scan_01TEST')).toBe(false);
  });

  test('progress events that are not terminal do nothing', async () => {
    const { native, orch } = setup();
    await orch.process('scan_01TEST');
    expect(orch.handleUploadState(native.jobs.get('scan_01TEST')!)).toBeUndefined();
  });

  test('expired session URIs are refreshed for just the expired paths', async () => {
    const { native, api, orch } = setup();
    await orch.process('scan_01TEST');
    const r = await orch.handleUploadState(native.expire('scan_01TEST', ['video/seg_000.mov']))!;
    expect(r.phase).toBe('uploading');
    expect(api.calls).toContain('refresh:srv_1:video/seg_000.mov');
    expect(native.calls).toContain('update:scan_01TEST:video/seg_000.mov');
    expect(native.jobs.get('scan_01TEST')!.state).toBe('uploading');
  });

  test('relaunch: upload finished while app was killed → resumeAll completes it', async () => {
    const { native, api, orch } = setup();
    await orch.process('scan_01TEST');
    native.finish('scan_01TEST');
    // New orchestrator instance = fresh JS context after relaunch.
    const orch2 = new UploadOrchestrator({ api, native, getWifiOnly: () => true });
    const results = await orch2.resumeAll();
    expect(results).toEqual([{ scanId: 'scan_01TEST', phase: 'completed' }]);
    expect(native.scans.size).toBe(0);
  });

  test('relaunch: native upload job lost → fresh URIs for every path and restart', async () => {
    const { native, api, orch } = setup({ phase: 'uploading', serverScanId: 'srv_1', uploadPaths: ['manifest.json', 'video/seg_000.mov'] });
    const r = await orch.process('scan_01TEST');
    expect(r.phase).toBe('uploading');
    expect(api.calls).toEqual(['refresh:srv_1:manifest.json,video/seg_000.mov']);
    expect(native.calls).toEqual(['start:scan_01TEST:manifest.json,video/seg_000.mov:wifi=true']);
  });

  test('crash between POST /scans and startUpload is recovered from the persisted creating phase', async () => {
    const { native, api, orch } = setup({ phase: 'creating' });
    const r = await orch.process('scan_01TEST');
    expect(r.phase).toBe('uploading');
    expect(api.calls).toEqual(['create:scan_01TEST']);
    expect(native.jobs.has('scan_01TEST')).toBe(true);
  });

  test('network failure on /complete keeps local files and retries later', async () => {
    const { native, api, orch } = setup();
    await orch.process('scan_01TEST');
    api.completeErrors.push(new ApiError('Network error', 0));
    const r = await orch.handleUploadState(native.finish('scan_01TEST'))!;
    expect(r.phase).toBe('completing');
    expect(native.scans.has('scan_01TEST')).toBe(true);
    expect(native.calls).not.toContain('delete:scan_01TEST');

    const again = await orch.resumeAll();
    expect(again[0].phase).toBe('completed');
    expect(native.scans.has('scan_01TEST')).toBe(false);
  });

  test('checksum mismatch (409) on /complete re-uploads instead of deleting', async () => {
    const { native, api, orch } = setup();
    await orch.process('scan_01TEST');
    native.finish('scan_01TEST');
    // Pretend one file was not actually done on the server side.
    native.jobs.get('scan_01TEST')!.files[1].status = 'failed';
    api.completeErrors.push(new ApiError('crc32c mismatch', 409));
    const r = await orch.process('scan_01TEST');
    expect(r.phase).toBe('uploading');
    expect(api.calls).toContain('refresh:srv_1:video/seg_000.mov');
    expect(native.scans.has('scan_01TEST')).toBe(true);
  });

  test('non-retryable create error marks failed; retry() starts over', async () => {
    const { native, api, orch } = setup();
    api.createError = new ApiError('bad manifest', 422);
    const r = await orch.process('scan_01TEST');
    expect(r).toMatchObject({ phase: 'failed', error: 'bad manifest' });
    expect(native.scans.get('scan_01TEST')!.meta.phase).toBe('failed');
    api.createError = undefined;
    const r2 = await orch.retry('scan_01TEST');
    expect(r2.phase).toBe('uploading');
  });

  test('retryable create error (5xx) keeps the scan recorded', async () => {
    const { native, api, orch } = setup();
    api.createError = new ApiError('unavailable', 503);
    const r = await orch.process('scan_01TEST');
    expect(r.phase).toBe('recorded');
    expect(native.scans.get('scan_01TEST')!.meta.error).toBe('unavailable');
  });

  test('concurrent process() calls share one run (no duplicate POST /scans)', async () => {
    const { api, orch } = setup();
    const [a, b] = await Promise.all([orch.process('scan_01TEST'), orch.process('scan_01TEST')]);
    expect(a).toBe(b);
    expect(api.calls.filter((c: string) => c.startsWith('create'))).toHaveLength(1);
  });

  test('scans still recording are left alone by resumeAll', async () => {
    const { api, orch } = setup({ phase: 'recording' });
    expect(await orch.resumeAll()).toEqual([]);
    expect(api.calls).toEqual([]);
  });

  test('wifiOnly setting is passed through to the native upload', async () => {
    const native = new FakeNative();
    native.addScan();
    const orch = new UploadOrchestrator({ api: new FakeApi(), native, getWifiOnly: async () => false });
    await orch.process('scan_01TEST');
    expect(native.calls[0]).toMatch(/wifi=false$/);
  });
});
