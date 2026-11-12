import { ApiClient, ApiError, normalizeBaseUrl } from '../api';

function mockFetch(status: number, body: unknown) {
  return jest.fn(async (_url: string, _init?: RequestInit) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: 'X',
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  })) as unknown as jest.Mock;
}

test('normalizeBaseUrl', () => {
  expect(normalizeBaseUrl('api.example.com')).toBe('https://api.example.com/v1');
  expect(normalizeBaseUrl('https://api.example.com/')).toBe('https://api.example.com/v1');
  expect(normalizeBaseUrl('https://api.example.com/v1/')).toBe('https://api.example.com/v1');
  expect(normalizeBaseUrl('  ')).toBe('');
});

test('createScan posts {manifest} with bearer token', async () => {
  const f = mockFetch(200, { scan_id: 's1', uploads: [] });
  const api = new ApiClient(() => ({ baseUrl: 'https://h', token: 'tok' }), f);
  const res = await api.createScan({ a: 1 });
  expect(res.scan_id).toBe('s1');
  const [url, init] = f.mock.calls[0];
  expect(url).toBe('https://h/v1/scans');
  expect(init.method).toBe('POST');
  expect(init.headers.Authorization).toBe('Bearer tok');
  expect(JSON.parse(init.body)).toEqual({ manifest: { a: 1 } });
});

test('push token + complete + uploads paths', async () => {
  const f = mockFetch(204, undefined);
  const api = new ApiClient(() => ({ baseUrl: 'https://h/v1', token: '' }), f);
  await api.registerPushToken('ExponentPushToken[x]');
  await api.complete('scan/1');
  await api.refreshUploads('s', ['a', 'b']);
  expect(f.mock.calls.map((c: unknown[]) => c[0])).toEqual([
    'https://h/v1/devices/push-token',
    'https://h/v1/scans/scan%2F1/complete',
    'https://h/v1/scans/s/uploads',
  ]);
  expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ expo_push_token: 'ExponentPushToken[x]' });
  expect(JSON.parse(f.mock.calls[2][1].body)).toEqual({ paths: ['a', 'b'] });
  expect(f.mock.calls[0][1].headers.Authorization).toBeUndefined();
});

test('errors carry status and retryability', async () => {
  const api = new ApiClient(() => ({ baseUrl: 'https://h', token: 't' }), mockFetch(409, { detail: 'crc mismatch' }));
  await expect(api.complete('s')).rejects.toMatchObject({ status: 409, retryable: false });
  const api2 = new ApiClient(() => ({ baseUrl: 'https://h', token: 't' }), jest.fn(async () => {
    throw new TypeError('Network request failed');
  }));
  const e = await api2.listScans().catch((x) => x);
  expect(e).toBeInstanceOf(ApiError);
  expect(e.status).toBe(0);
  expect(e.retryable).toBe(true);
  const api3 = new ApiClient(() => ({ baseUrl: '', token: '' }), mockFetch(200, []));
  await expect(api3.listScans()).rejects.toThrow(/not set/);
});
