import { router } from 'expo-router';
import * as React from 'react';
import { ActivityIndicator, Alert, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import {
  CapturePreview,
  ScannerCapture,
  type CaptureMap,
  type CaptureStatus,
  type CaptureWarning,
} from '../modules/scanner-capture';
import { Minimap } from '../src/components/Minimap';
import { Button } from '../src/components/ui';
import { C } from '../src/components/theme';
import { estimateUploadBytes, formatBytes, formatDuration } from '../src/lib/scans';
import { useApp } from '../src/state/AppContext';

const WARNING_TEXT: Record<CaptureWarning, string> = {
  fast_motion: 'Slow down: moving too fast',
  low_light: 'Too dark: turn on lights',
  tracking_limited: 'Tracking limited',
  tracking_lost: 'Tracking lost: go back to a known area',
  featureless: 'Blank surface: point at textured areas',
  too_close: 'Too close: step back',
  thermal_serious: 'Phone is hot: stills paused',
  thermal_critical: 'Phone overheating: stop soon',
  storage_low: 'Storage almost full',
};

export default function CaptureScreen() {
  const { supported } = useApp();
  if (!supported || !ScannerCapture) return <Unsupported />;
  return <Recorder />;
}

function Unsupported() {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.center, { paddingTop: insets.top + 24, paddingBottom: insets.bottom + 24 }]} testID="unsupported">
      <Text style={{ fontSize: 56 }}>📵</Text>
      <Text style={styles.title}>LiDAR iPhone (Pro) required</Text>
      <Text style={styles.body}>
        Scanner records LiDAR depth with ARKit scene depth. This device doesn&apos;t support it. Use an iPhone 12 Pro or
        later Pro model (target: iPhone 15 Pro).
      </Text>
      <Button title="Back to scans" kind="secondary" onPress={() => router.back()} style={{ alignSelf: 'stretch' }} />
    </View>
  );
}

function Recorder() {
  const native = ScannerCapture!;
  const { settings, orchestrator, configured, refresh } = useApp();
  const insets = useSafeAreaInsets();
  const [status, setStatus] = React.useState<CaptureStatus>();
  const [map, setMap] = React.useState<CaptureMap>();
  const [name, setName] = React.useState(() => `Scan ${new Date().toLocaleDateString()}`);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [sessionError, setSessionError] = React.useState<string>();
  const stopping = React.useRef(false);

  const state = status?.state ?? 'idle';
  const recording = state === 'recording' || state === 'paused';

  React.useEffect(() => {
    const subs = [
      native.addListener('onStatus', setStatus),
      native.addListener('onMap', setMap),
      native.addListener('onCaptureEvent', (e) => {
        if (e.type === 'error') Alert.alert('Capture error', String(e.message));
        if (e.type === 'interrupted') Alert.alert('Recording paused', 'The camera was interrupted. Tap resume.');
      }),
    ];
    native.startSession().catch((e: Error) => setSessionError(e.message));
    return () => {
      subs.forEach((s) => s.remove());
      native.stopSession().catch(() => {});
    };
  }, [native]);

  const stop = React.useCallback(async () => {
    if (stopping.current) return;
    stopping.current = true;
    setBusy('Finalising scan (checksums)…');
    try {
      const scan = await native.stopRecording();
      await refresh();
      if (configured) orchestrator.process(scan.scanId).finally(refresh);
      router.replace({ pathname: '/scan/[id]', params: { id: scan.scanId, local: '1' } });
    } catch (e) {
      Alert.alert('Stop failed', (e as Error).message);
    } finally {
      setBusy(null);
      stopping.current = false;
    }
  }, [native, orchestrator, configured, refresh]);

  // Hard stop at the configured limit (R3.10).
  const limitS = settings.maxDurationMin * 60;
  React.useEffect(() => {
    if (status && recording && status.elapsedS >= limitS) {
      stop().then(() => Alert.alert('Time limit reached', `Recording stopped at ${settings.maxDurationMin} min.`));
    }
  }, [status, recording, limitS, stop, settings.maxDurationMin]);

  const onRecord = async () => {
    try {
      if (!recording) {
        setBusy('Starting…');
        await native.startRecording({ name: name.trim() || 'Scan', stills: settings.hiResStills });
      } else {
        await stop();
      }
    } catch (e) {
      Alert.alert('Error', (e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const onPause = async () => {
    try {
      if (state === 'recording') await native.pauseRecording();
      else if (state === 'paused') await native.resumeRecording();
    } catch (e) {
      Alert.alert('Error', (e as Error).message);
    }
  };

  const onClose = () => {
    if (!recording) return router.back();
    Alert.alert('Recording in progress', 'Stop and save, or discard this scan?', [
      { text: 'Keep recording', style: 'cancel' },
      { text: 'Discard', style: 'destructive', onPress: () => native.discardRecording().then(() => router.back()) },
      { text: 'Stop & save', onPress: stop },
    ]);
  };

  const warnings = status?.warnings ?? [];
  const nearLimit = recording && status && status.elapsedS > limitS * 0.9;
  const est = status ? estimateUploadBytes(status.bytesWritten, status.elapsedS) : 0;

  return (
    <View style={{ flex: 1, backgroundColor: '#000' }}>
      <CapturePreview style={StyleSheet.absoluteFill} showMesh={settings.showMesh} />

      {/* Top bar */}
      <View style={[styles.top, { paddingTop: insets.top + 6 }]}>
        <Pressable onPress={onClose} hitSlop={12}>
          <Text style={styles.close}>✕</Text>
        </Pressable>
        {recording ? (
          <View style={{ flex: 1, alignItems: 'center' }}>
            <Text style={[styles.timer, state === 'paused' && { color: C.warn }]}>
              {state === 'paused' ? '❚❚ ' : '● '}
              {formatDuration(status?.elapsedS)}
            </Text>
            <Text style={styles.stat}>
              {(status?.distanceM ?? 0).toFixed(1)} m{settings.hiResStills ? ` · ${status?.stills ?? 0} photos` : ''} · seg {status?.segments ?? 0}
            </Text>
          </View>
        ) : (
          <TextInput
            value={name}
            onChangeText={setName}
            style={styles.nameInput}
            placeholder="Scan name"
            placeholderTextColor="#aaa"
            returnKeyType="done"
          />
        )}
        <View style={{ alignItems: 'flex-end', minWidth: 80 }}>
          <Text style={styles.stat}>{status ? `${status.fps.toFixed(0)} fps` : '—'}</Text>
          <Text style={[styles.stat, { color: status?.tracking === 'normal' ? C.ok : C.warn }]}>
            {status?.tracking.replace('limited:', '') ?? 'starting'}
          </Text>
        </View>
      </View>

      {/* Storage line */}
      <View style={styles.storage}>
        <Text style={styles.stat}>
          {formatBytes(status?.bytesWritten)} written · ~{formatBytes(est)} upload · {formatBytes(status?.freeBytes)} free
        </Text>
      </View>

      {/* Warnings */}
      <View style={styles.warnings} pointerEvents="none">
        {sessionError ? <Text style={styles.warning}>{sessionError}</Text> : null}
        {nearLimit ? <Text style={styles.warning}>Time limit in {formatDuration(limitS - (status?.elapsedS ?? 0))}</Text> : null}
        {warnings.map((w) => (
          <Text key={w} style={styles.warning}>
            {WARNING_TEXT[w] ?? w}
          </Text>
        ))}
      </View>

      {/* Bottom controls */}
      <View style={[styles.bottom, { paddingBottom: insets.bottom + 16 }]}>
        <Minimap map={map} pose={status?.pose} size={120} />
        <Pressable
          onPress={onRecord}
          disabled={!!busy || state === 'finishing'}
          style={styles.recordOuter}
          accessibilityLabel={recording ? 'Stop recording' : 'Start recording'}
        >
          <View style={[styles.recordInner, recording && styles.recordInnerStop]} />
        </Pressable>
        <View style={{ width: 120, alignItems: 'center' }}>
          {recording ? (
            <Pressable onPress={onPause} style={styles.pauseBtn}>
              <Text style={{ color: '#fff', fontSize: 16, fontWeight: '700' }}>{state === 'paused' ? 'Resume' : 'Pause'}</Text>
            </Pressable>
          ) : null}
        </View>
      </View>

      {busy ? (
        <View style={styles.busy}>
          <ActivityIndicator color="#fff" />
          <Text style={{ color: '#fff', marginTop: 8 }}>{busy}</Text>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center', padding: 28, gap: 16 },
  title: { color: C.text, fontSize: 24, fontWeight: '800', textAlign: 'center' },
  body: { color: C.dim, fontSize: 15, textAlign: 'center', lineHeight: 22 },
  top: {
    position: 'absolute',
    left: 0,
    right: 0,
    top: 0,
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingBottom: 8,
    backgroundColor: 'rgba(0,0,0,0.45)',
    gap: 12,
  },
  close: { color: '#fff', fontSize: 22, width: 28 },
  timer: { color: '#fff', fontSize: 22, fontWeight: '800', fontVariant: ['tabular-nums'] },
  stat: { color: '#ddd', fontSize: 12, fontVariant: ['tabular-nums'] },
  nameInput: {
    flex: 1,
    color: '#fff',
    fontSize: 16,
    borderBottomWidth: 1,
    borderColor: 'rgba(255,255,255,0.3)',
    paddingVertical: 4,
  },
  storage: { position: 'absolute', top: 110, left: 0, right: 0, alignItems: 'center' },
  warnings: { position: 'absolute', top: 140, left: 16, right: 16, alignItems: 'center', gap: 6 },
  warning: {
    color: '#000',
    backgroundColor: C.warn,
    fontWeight: '700',
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 8,
    overflow: 'hidden',
  },
  bottom: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
  },
  recordOuter: {
    width: 88,
    height: 88,
    borderRadius: 44,
    borderWidth: 5,
    borderColor: '#fff',
    alignItems: 'center',
    justifyContent: 'center',
  },
  recordInner: { width: 68, height: 68, borderRadius: 34, backgroundColor: C.record },
  recordInnerStop: { width: 34, height: 34, borderRadius: 6 },
  pauseBtn: { backgroundColor: 'rgba(0,0,0,0.6)', paddingHorizontal: 18, paddingVertical: 12, borderRadius: 24 },
  busy: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(0,0,0,0.6)', alignItems: 'center', justifyContent: 'center' },
});
