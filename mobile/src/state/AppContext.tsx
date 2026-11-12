import * as React from 'react';
import { AppState } from 'react-native';

import {
  ScannerCapture,
  type LocalScan,
  type UploadProgress,
  type UploadState,
} from '../../modules/scanner-capture';
import { ApiClient, type RemoteScan } from '../lib/api';
import { getNetState, subscribeNet, type NetState } from '../lib/network';
import { UploadOrchestrator, type NativeUploads } from '../lib/orchestrator';
import { registerForPush } from '../lib/push';
import { mergeScans, type ScanRow } from '../lib/scans';
import { DEFAULT_SETTINGS, loadSettings, saveSettings, type Settings } from '../lib/settings';

type Ctx = {
  ready: boolean;
  supported: boolean;
  settings: Settings;
  updateSettings: (s: Settings) => Promise<void>;
  api: ApiClient;
  orchestrator: UploadOrchestrator;
  rows: ScanRow[];
  local: LocalScan[];
  remote: RemoteScan[];
  remoteError?: string;
  progress: Record<string, UploadProgress | undefined>;
  uploadStates: Record<string, UploadState | undefined>;
  net: NetState;
  refresh: () => Promise<void>;
  configured: boolean;
};

const AppCtx = React.createContext<Ctx | null>(null);

const noopNative: NativeUploads = {
  listLocalScans: async () => [],
  getLocalScan: async () => null,
  readManifest: async () => null,
  setScanMeta: async () => {},
  deleteLocalScan: async () => {},
  startUpload: async () => {},
  updateUploadUrls: async () => {},
  getUploadState: async () => null,
  forgetUpload: async () => {},
};

/** Latest settings, readable from callbacks that outlive a render (API client, orchestrator). */
const live: { settings: Settings } = { settings: DEFAULT_SETTINGS };

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [settings, setSettingsState] = React.useState<Settings>(DEFAULT_SETTINGS);
  const setSettings = React.useCallback((s: Settings) => {
    live.settings = s;
    setSettingsState(s);
  }, []);
  const [ready, setReady] = React.useState(false);
  const [local, setLocal] = React.useState<LocalScan[]>([]);
  const [remote, setRemote] = React.useState<RemoteScan[]>([]);
  const [remoteError, setRemoteError] = React.useState<string>();
  const [progress, setProgress] = React.useState<Record<string, UploadProgress | undefined>>({});
  const [uploadStates, setUploadStates] = React.useState<Record<string, UploadState | undefined>>({});
  const [net, setNet] = React.useState<NetState>({ connected: true, wifi: true });

  const supported = React.useMemo(() => {
    try {
      return ScannerCapture?.isSupported() ?? false;
    } catch {
      return false;
    }
  }, []);

  const api = React.useMemo(
    () => new ApiClient(() => ({ baseUrl: live.settings.apiBaseUrl, token: live.settings.apiToken })),
    [],
  );
  const orchestrator = React.useMemo(
    () =>
      new UploadOrchestrator({
        api,
        native: (ScannerCapture as unknown as NativeUploads) ?? noopNative,
        getWifiOnly: () => live.settings.wifiOnly,
        log: (m) => console.log(m),
      }),
    [api],
  );
  const configured = !!settings.apiBaseUrl;

  const refresh = React.useCallback(async () => {
    if (ScannerCapture) {
      const [scans, states] = await Promise.all([
        ScannerCapture.listLocalScans().catch(() => []),
        ScannerCapture.getAllUploadStates().catch(() => []),
      ]);
      setLocal(scans);
      setUploadStates(Object.fromEntries(states.map((s) => [s.scanId, s])));
      setProgress((p) => {
        const n = { ...p };
        for (const s of states) n[s.scanId] = s;
        return n;
      });
    }
    if (live.settings.apiBaseUrl) {
      try {
        setRemote(await api.listScans());
        setRemoteError(undefined);
      } catch (e) {
        setRemoteError((e as Error).message);
      }
    } else {
      setRemote([]);
    }
  }, [api]);

  const kick = React.useCallback(async () => {
    if (!live.settings.apiBaseUrl) return;
    await orchestrator.resumeAll().catch((e) => console.warn(e));
    await refresh();
  }, [orchestrator, refresh]);

  // Load settings, then first refresh + resume.
  React.useEffect(() => {
    (async () => {
      const s = await loadSettings();
      setSettings(s);
      setReady(true);
      setNet(await getNetState());
      await ScannerCapture?.resumeUploads().catch(() => {});
      await refresh();
      if (s.apiBaseUrl) {
        kick();
        registerForPush(api);
      }
    })();
  }, [api, kick, refresh, setSettings]);

  // Native events.
  React.useEffect(() => {
    if (!ScannerCapture) return;
    const subs = [
      ScannerCapture.addListener('onUploadProgress', (p) => setProgress((m) => ({ ...m, [p.scanId]: p }))),
      ScannerCapture.addListener('onUploadState', (s) => {
        setUploadStates((m) => ({ ...m, [s.scanId]: s }));
        orchestrator.handleUploadState(s)?.then(() => refresh());
      }),
    ];
    const off = orchestrator.onChange(() => refresh());
    return () => {
      subs.forEach((s) => s.remove());
      off();
    };
  }, [orchestrator, refresh]);

  // Foreground / network changes → resume.
  React.useEffect(() => {
    const sub = AppState.addEventListener('change', (st) => {
      if (st === 'active') {
        ScannerCapture?.resumeUploads().catch(() => {});
        kick();
      }
    });
    const offNet = subscribeNet((n) => {
      setNet((prev) => {
        if (n.connected && (!prev.connected || n.wifi !== prev.wifi)) kick();
        return n;
      });
    });
    return () => {
      sub.remove();
      offNet();
    };
  }, [kick]);

  const updateSettings = React.useCallback(
    async (s: Settings) => {
      await saveSettings(s);
      const wifiChanged = s.wifiOnly !== live.settings.wifiOnly;
      setSettings(s);
      if (wifiChanged) await ScannerCapture?.setWifiOnly(s.wifiOnly).catch(() => {});
      if (s.apiBaseUrl) {
        registerForPush(api);
        kick();
      } else {
        refresh();
      }
    },
    [api, kick, refresh, setSettings],
  );

  const rows = React.useMemo(() => mergeScans(local, remote, progress), [local, remote, progress]);

  const value: Ctx = {
    ready,
    supported,
    settings,
    updateSettings,
    api,
    orchestrator,
    rows,
    local,
    remote,
    remoteError,
    progress,
    uploadStates,
    net,
    refresh,
    configured,
  };
  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}

export function useApp(): Ctx {
  const c = React.useContext(AppCtx);
  if (!c) throw new Error('useApp outside AppProvider');
  return c;
}
