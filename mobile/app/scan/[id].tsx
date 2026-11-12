import * as WebBrowser from 'expo-web-browser';
import { Stack, router, useLocalSearchParams } from 'expo-router';
import * as React from 'react';
import { Alert, Image, ScrollView, StyleSheet, Text, View } from 'react-native';

import { ScannerCapture } from '../../modules/scanner-capture';
import { Banner, Button, ProgressBar, StatusPill } from '../../src/components/ui';
import { C } from '../../src/components/theme';
import type { RemoteScanDetail } from '../../src/lib/api';
import { formatBytes, formatDuration, remoteLabel, uploadFraction } from '../../src/lib/scans';
import { useApp } from '../../src/state/AppContext';

const STAGES = ['ingest', 'frames', 'sfm', 'depth', 'mesh', 'plan', 'publish']; // splat + lod disabled for now

export default function ScanDetail() {
  const { id, local: isLocalParam } = useLocalSearchParams<{ id: string; local?: string }>();
  const { local, progress, uploadStates, api, configured, orchestrator, refresh, settings, net } = useApp();
  const scan = local.find((s) => s.scanId === id);
  // A local capture id is never a server id: until the local list has loaded (or before the upload has been
  // registered) there is nothing to poll. Only remote-only rows pass their server id as the route id.
  const isLocal = isLocalParam === '1' || !!scan;
  const serverId = scan?.meta.serverScanId ?? (isLocal ? undefined : id);
  const [remote, setRemote] = React.useState<RemoteScanDetail>();
  const [err, setErr] = React.useState<string>();

  // Poll the server status while processing.
  React.useEffect(() => {
    if (!serverId || !configured) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const r = await api.getScan(serverId);
        if (!alive) return;
        setRemote(r);
        setErr(undefined);
        if (r.status === 'ready' || r.status === 'failed') return;
      } catch (e) {
        if (alive) setErr((e as Error).message);
      }
      timer = setTimeout(tick, 5000);
    };
    tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [serverId, configured, api]);

  const p = scan ? progress[scan.scanId] : undefined;
  const us = scan ? uploadStates[scan.scanId] : undefined;
  const frac = uploadFraction(p);
  const phase = scan?.meta.phase;

  const upload = async () => {
    if (!scan) return;
    if (!configured) return Alert.alert('Not configured', 'Set the API URL in Settings first.');
    const r = phase === 'failed' ? await orchestrator.retry(scan.scanId) : await orchestrator.process(scan.scanId);
    if (r.error) Alert.alert('Upload', r.error);
    refresh();
  };

  const del = () =>
    Alert.alert('Delete local copy?', 'The recording has not been confirmed by the server and will be lost.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Delete',
        style: 'destructive',
        onPress: async () => {
          await ScannerCapture?.deleteLocalScan(scan!.scanId);
          await refresh();
          router.back();
        },
      },
    ]);

  return (
    <ScrollView style={{ flex: 1, backgroundColor: C.bg }} contentContainerStyle={{ padding: 16, gap: 14 }}>
      <Stack.Screen options={{ title: scan?.name ?? remote?.name ?? 'Scan' }} />
      {scan?.thumbnailUri || remote?.thumbnail_url ? (
        <Image source={{ uri: scan?.thumbnailUri ?? remote!.thumbnail_url! }} style={styles.hero} resizeMode="cover" />
      ) : null}

      {scan ? (
        <View style={styles.card}>
          <Text style={styles.h}>On this phone</Text>
          <Text style={styles.t}>
            {scan.createdAt ? new Date(scan.createdAt).toLocaleString() : ''} · {formatDuration(scan.durationS)} ·{' '}
            {formatBytes(scan.sizeBytes)}
          </Text>
          <StatusPill label={phaseLabel(phase)} status={phase === 'failed' ? 'failed' : 'uploading'} />
          {p ? (
            <>
              <ProgressBar value={frac ?? 0} />
              <Text style={styles.t}>
                {formatBytes(p.sentBytes)} / {formatBytes(p.totalBytes)} · {p.filesDone}/{p.filesTotal} files
              </Text>
            </>
          ) : null}
          {settings.wifiOnly && !net.wifi && phase === 'uploading' ? (
            <Banner tone="info" text="Waiting for Wi-Fi (Wi-Fi only upload is on)." />
          ) : null}
          {scan.meta.error ? <Banner tone="bad" text={String(scan.meta.error)} /> : null}
          {us?.state === 'error' && us.error ? <Banner tone="bad" text={us.error} /> : null}
          <View style={{ gap: 8, marginTop: 6 }}>
            {phase === 'recorded' || phase === 'failed' || !phase ? (
              <Button title={phase === 'failed' ? 'Retry upload' : 'Upload'} onPress={upload} />
            ) : (
              <Button title="Check upload" kind="secondary" onPress={upload} />
            )}
            <Button title="Delete local copy" kind="danger" onPress={del} />
          </View>
        </View>
      ) : null}

      {serverId ? (
        <View style={styles.card}>
          <Text style={styles.h}>Server</Text>
          {err ? <Banner tone="bad" text={err} /> : null}
          {remote ? (
            <>
              <StatusPill label={remoteLabel(remote)} status={remote.status} />
              {typeof remote.progress === 'number' && remote.status === 'processing' ? (
                <ProgressBar value={remote.progress} />
              ) : null}
              <View style={{ marginTop: 6, gap: 4 }}>
                {STAGES.map((s) => {
                  const st = remote.stages?.[s];
                  return (
                    <View key={s} style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
                      <Text style={styles.t}>{s}</Text>
                      <Text style={[styles.t, { color: stageColor(st?.status) }]}>
                        {st?.status ?? '—'}
                        {st?.error ? ` · ${st.error}` : ''}
                      </Text>
                    </View>
                  );
                })}
              </View>
              {remote.viewer_url ? (
                <Button title="Open in viewer" onPress={() => WebBrowser.openBrowserAsync(remote.viewer_url!)} />
              ) : null}
            </>
          ) : (
            <Text style={styles.t}>{configured ? 'Loading…' : 'API not configured'}</Text>
          )}
          <Text style={[styles.t, { fontSize: 11 }]}>{serverId}</Text>
        </View>
      ) : null}
    </ScrollView>
  );
}

function phaseLabel(p?: string) {
  switch (p) {
    case 'creating':
      return 'Registering with server';
    case 'uploading':
      return 'Uploading';
    case 'completing':
      return 'Verifying upload';
    case 'failed':
      return 'Upload failed';
    default:
      return 'Recorded (not uploaded)';
  }
}

function stageColor(s?: string) {
  if (s === 'done' || s === 'succeeded' || s === 'ready') return C.ok;
  if (s === 'failed') return C.bad;
  if (s === 'running') return C.accent;
  return C.dim;
}

const styles = StyleSheet.create({
  hero: { width: '100%', height: 220, borderRadius: 14, backgroundColor: '#000' },
  card: { backgroundColor: C.card, borderRadius: 14, padding: 14, gap: 8, borderWidth: 1, borderColor: C.border },
  h: { color: C.dim, fontSize: 12, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 1 },
  t: { color: C.text, fontSize: 13 },
});
