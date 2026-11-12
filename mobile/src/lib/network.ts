import * as Network from 'expo-network';

export type NetState = { connected: boolean; wifi: boolean };

export async function getNetState(): Promise<NetState> {
  try {
    const s = await Network.getNetworkStateAsync();
    return { connected: !!s.isConnected && s.isInternetReachable !== false, wifi: s.type === Network.NetworkStateType.WIFI };
  } catch {
    return { connected: true, wifi: true };
  }
}

export function subscribeNet(fn: (s: NetState) => void): () => void {
  try {
    const sub = Network.addNetworkStateListener((s) =>
      fn({ connected: !!s.isConnected && s.isInternetReachable !== false, wifi: s.type === Network.NetworkStateType.WIFI }),
    );
    return () => sub.remove();
  } catch {
    return () => {};
  }
}
