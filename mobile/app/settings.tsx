import * as React from 'react';
import { Alert, ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';

import { ScannerCapture } from '../modules/scanner-capture';
import { Button } from '../src/components/ui';
import { C } from '../src/components/theme';
import { normalizeBaseUrl } from '../src/lib/api';
import { formatBytes } from '../src/lib/scans';
import { useApp } from '../src/state/AppContext';

export default function SettingsScreen() {
  const { settings, updateSettings, api } = useApp();
  // Draft is re-seeded whenever the saved settings object changes (e.g. after the initial load).
  const [seed, setSeed] = React.useState(settings);
  const [draft, setDraft] = React.useState(settings);
  if (seed !== settings) {
    setSeed(settings);
    setDraft(settings);
  }
  const [testing, setTesting] = React.useState(false);
  const info = React.useMemo(() => {
    try {
      return ScannerCapture?.getDeviceInfo() ?? null;
    } catch {
      return null;
    }
  }, []);

  const save = async () => {
    const next = { ...draft, apiBaseUrl: draft.apiBaseUrl ? normalizeBaseUrl(draft.apiBaseUrl) : '' };
    await updateSettings(next);
    Alert.alert('Saved');
  };

  const test = async () => {
    setTesting(true);
    try {
      await updateSettings({ ...draft, apiBaseUrl: normalizeBaseUrl(draft.apiBaseUrl) });
      const scans = await api.listScans();
      Alert.alert('Connected', `${scans.length} scan(s) on the server.`);
    } catch (e) {
      Alert.alert('Connection failed', (e as Error).message);
    } finally {
      setTesting(false);
    }
  };

  return (
    <ScrollView style={{ flex: 1, backgroundColor: C.bg }} contentContainerStyle={{ padding: 16, gap: 18 }}>
      <Section title="Server">
        <Label text="API base URL" />
        <TextInput
          style={styles.input}
          value={draft.apiBaseUrl}
          onChangeText={(apiBaseUrl) => setDraft({ ...draft, apiBaseUrl })}
          placeholder="https://scanner-api-xxxx.run.app/v1"
          placeholderTextColor={C.dim}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          testID="apiBaseUrl"
        />
        <Label text="API token (stored in the iOS Keychain)" />
        <TextInput
          style={styles.input}
          value={draft.apiToken}
          onChangeText={(apiToken) => setDraft({ ...draft, apiToken })}
          placeholder="Bearer token"
          placeholderTextColor={C.dim}
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry
          testID="apiToken"
        />
      </Section>

      <Section title="Upload">
        <Toggle
          label="Upload on Wi-Fi only"
          value={draft.wifiOnly}
          onChange={(wifiOnly) => setDraft({ ...draft, wifiOnly })}
        />
      </Section>

      <Section title="Capture">
        <Toggle
          label="Show mesh overlay"
          value={draft.showMesh}
          onChange={(showMesh) => setDraft({ ...draft, showMesh })}
        />
        <Toggle
          label="High-res stills (may disrupt video)"
          value={draft.hiResStills}
          onChange={(hiResStills) => setDraft({ ...draft, hiResStills })}
        />
        <Label text="Max recording length (minutes)" />
        <TextInput
          style={styles.input}
          value={String(draft.maxDurationMin)}
          onChangeText={(t) => setDraft({ ...draft, maxDurationMin: Math.max(1, Math.min(120, parseInt(t, 10) || 30)) })}
          keyboardType="number-pad"
        />
      </Section>

      <View style={{ gap: 10 }}>
        <Button title="Save" onPress={save} />
        <Button title={testing ? 'Testing…' : 'Save & test connection'} kind="secondary" onPress={test} disabled={testing} />
      </View>

      <Section title="Device">
        <Text style={styles.small}>
          {info
            ? `${info.model} · ${info.os} · app ${info.appVersion}\nLiDAR depth: ${info.supported ? 'yes' : 'no'} · mesh: ${
                info.supportsMesh ? 'yes' : 'no'
              }\nVideo: ${info.videoFormat ? `${info.videoFormat.w}×${info.videoFormat.h} @ ${info.videoFormat.fps} fps` : '—'}\nFree: ${formatBytes(
                info.freeBytes,
              )}`
            : 'Native capture module not available'}
        </Text>
      </Section>
    </ScrollView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {children}
    </View>
  );
}
const Label = ({ text }: { text: string }) => <Text style={styles.label}>{text}</Text>;
function Toggle({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
      <Text style={{ color: C.text, fontSize: 15 }}>{label}</Text>
      <Switch value={value} onValueChange={onChange} />
    </View>
  );
}

const styles = StyleSheet.create({
  section: { backgroundColor: C.card, borderRadius: 14, padding: 14, gap: 8, borderWidth: 1, borderColor: C.border },
  sectionTitle: { color: C.dim, fontSize: 12, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 1 },
  label: { color: C.dim, fontSize: 13, marginTop: 4 },
  input: {
    color: C.text,
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 15,
    backgroundColor: '#0f1216',
  },
  small: { color: C.text, fontSize: 13, lineHeight: 20 },
});
