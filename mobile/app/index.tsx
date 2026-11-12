import { Link, Stack, router } from 'expo-router';
import * as React from 'react';
import { FlatList, Image, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ScannerCapture } from '../modules/scanner-capture';
import { Banner, Button, ProgressBar, StatusPill } from '../src/components/ui';
import { C } from '../src/components/theme';
import { formatBytes, type ScanRow } from '../src/lib/scans';
import { useApp } from '../src/state/AppContext';

export default function ScanList() {
  const { rows, refresh, configured, supported, remoteError, net, settings } = useApp();
  const [refreshing, setRefreshing] = React.useState(false);
  const insets = useSafeAreaInsets();

  // Simulator test hook (see ScannerCaptureModule.initialRoute).
  React.useEffect(() => {
    const route = ScannerCapture?.initialRoute?.();
    if (route === '/capture' || route === '/settings') router.push(route);
  }, []);

  const onRefresh = React.useCallback(async () => {
    setRefreshing(true);
    await refresh();
    setRefreshing(false);
  }, [refresh]);

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <Stack.Screen
        options={{
          headerRight: () => (
            <Link href="/settings" asChild>
              <Pressable hitSlop={12} style={{ minWidth: 88, paddingHorizontal: 12, alignItems: 'center' }}>
                <Text numberOfLines={1} style={{ color: C.accent, fontSize: 16 }}>Settings</Text>
              </Pressable>
            </Link>
          ),
        }}
      />
      <FlatList
        data={rows}
        keyExtractor={(r) => r.key}
        contentContainerStyle={{ padding: 16, paddingBottom: 120 }}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.dim} />}
        ListHeaderComponent={
          <View>
            {!supported && <Banner tone="bad" text="This device has no LiDAR. Recording needs an iPhone Pro (12 Pro or later)." />}
            {!configured && <Banner text="Set the API URL in Settings to upload scans." />}
            {configured && remoteError ? <Banner tone="bad" text={`Server: ${remoteError}`} /> : null}
            {settings.wifiOnly && !net.wifi && rows.some((r) => r.status === 'uploading') ? (
              <Banner tone="info" text="Waiting for Wi-Fi to upload (Wi-Fi only is on)." />
            ) : null}
          </View>
        }
        ListEmptyComponent={
          <Text style={{ color: C.dim, textAlign: 'center', marginTop: 48 }}>No scans yet. Tap New scan to record.</Text>
        }
        renderItem={({ item }) => <Row row={item} />}
      />
      <View style={[styles.footer, { paddingBottom: insets.bottom + 12 }]}>
        <Button title="New scan" onPress={() => router.push('/capture')} />
      </View>
    </View>
  );
}

function Row({ row }: { row: ScanRow }) {
  const id = row.localId ?? row.serverId!;
  return (
    <Pressable
      onPress={() => router.push({ pathname: '/scan/[id]', params: { id, local: row.localId ? '1' : '0' } })}
      style={({ pressed }) => [styles.row, pressed && { opacity: 0.7 }]}
    >
      {row.thumbnailUri ? (
        <Image source={{ uri: row.thumbnailUri }} style={styles.thumb} />
      ) : (
        <View style={[styles.thumb, { alignItems: 'center', justifyContent: 'center' }]}>
          <Text style={{ color: C.dim, fontSize: 22 }}>▢</Text>
        </View>
      )}
      <View style={{ flex: 1, gap: 4 }}>
        <Text style={styles.name} numberOfLines={1}>
          {row.name}
        </Text>
        <Text style={styles.sub}>
          {row.createdAt ? new Date(row.createdAt).toLocaleString() : '—'}
          {row.sizeBytes ? ` · ${formatBytes(row.sizeBytes)}` : ''}
        </Text>
        <StatusPill label={row.statusLabel} status={row.status} />
        {row.progress !== undefined && row.status !== 'ready' ? <ProgressBar value={row.progress} /> : null}
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    gap: 12,
    padding: 12,
    backgroundColor: C.card,
    borderRadius: 14,
    marginBottom: 10,
    borderWidth: 1,
    borderColor: C.border,
  },
  thumb: { width: 64, height: 86, borderRadius: 8, backgroundColor: '#000' },
  name: { color: C.text, fontSize: 16, fontWeight: '600' },
  sub: { color: C.dim, fontSize: 12 },
  footer: { position: 'absolute', left: 16, right: 16, bottom: 0 },
});
