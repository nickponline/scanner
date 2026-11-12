import * as SecureStore from 'expo-secure-store';

export type Settings = {
  apiBaseUrl: string;
  apiToken: string;
  wifiOnly: boolean;
  maxDurationMin: number;
  showMesh: boolean;
  /** ARKit high-res stills alongside the video. Off by default: each grab reconfigures the camera, which can stall
   *  the 4K video and jump exposure/focus; the pipeline then works from video frames only. */
  hiResStills: boolean;
};

export const DEFAULT_SETTINGS: Settings = {
  apiBaseUrl: 'https://scanner-api-529706408006.us-central1.run.app/v1',
  apiToken: '',
  wifiOnly: true,
  maxDurationMin: 30,
  showMesh: true,
  hiResStills: false,
};

const TOKEN_KEY = 'scanner.apiToken';
const PREFS_KEY = 'scanner.prefs';

export async function loadSettings(): Promise<Settings> {
  try {
    const [token, prefs] = await Promise.all([
      SecureStore.getItemAsync(TOKEN_KEY),
      SecureStore.getItemAsync(PREFS_KEY),
    ]);
    const p = prefs ? (JSON.parse(prefs) as Partial<Settings>) : {};
    return { ...DEFAULT_SETTINGS, ...p, apiBaseUrl: p.apiBaseUrl || DEFAULT_SETTINGS.apiBaseUrl, apiToken: token ?? '' };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

export async function saveSettings(s: Settings): Promise<void> {
  const { apiToken, ...prefs } = s;
  await SecureStore.setItemAsync(PREFS_KEY, JSON.stringify(prefs));
  if (apiToken) {
    // Token only readable after first unlock (background refreshes still work).
    await SecureStore.setItemAsync(TOKEN_KEY, apiToken, {
      keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
    });
  } else {
    await SecureStore.deleteItemAsync(TOKEN_KEY);
  }
}
