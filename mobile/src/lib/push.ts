import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';

import type { ApiClient } from './api';

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: false,
    shouldSetBadge: false,
  }),
});

/** Best-effort: ask permission, get an Expo push token and register it with the API. Never throws. */
export async function registerForPush(api: ApiClient, log: (m: string) => void = console.log): Promise<string | null> {
  try {
    if (!Device.isDevice) return null;
    const perm = await Notifications.getPermissionsAsync();
    let granted = perm.granted;
    if (!granted && perm.canAskAgain) granted = (await Notifications.requestPermissionsAsync()).granted;
    if (!granted) return null;
    const projectId =
      (Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined)?.eas?.projectId ??
      Constants.easConfig?.projectId;
    if (!projectId) {
      log('[push] no EAS projectId configured; skipping push registration');
      return null;
    }
    const { data } = await Notifications.getExpoPushTokenAsync({ projectId });
    await api.registerPushToken(data);
    return data;
  } catch (e) {
    log(`[push] registration failed: ${(e as Error).message}`);
    return null;
  }
}
