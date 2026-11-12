import * as React from 'react';
import { Pressable, StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';

import { C } from './theme';

export function Button({
  title,
  onPress,
  kind = 'primary',
  disabled,
  style,
}: {
  title: string;
  onPress: () => void;
  kind?: 'primary' | 'secondary' | 'danger';
  disabled?: boolean;
  style?: StyleProp<ViewStyle>;
}) {
  const bg = kind === 'primary' ? C.accent : kind === 'danger' ? C.bad : C.card;
  const fg = kind === 'primary' ? '#001018' : C.text;
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [
        s.btn,
        { backgroundColor: bg, opacity: disabled ? 0.4 : pressed ? 0.75 : 1 },
        kind === 'secondary' && { borderWidth: 1, borderColor: C.border },
        style,
      ]}
    >
      <Text style={[s.btnText, { color: fg }]}>{title}</Text>
    </Pressable>
  );
}

export function Banner({ text, tone = 'warn' }: { text: string; tone?: 'warn' | 'bad' | 'info' }) {
  const color = tone === 'bad' ? C.bad : tone === 'info' ? C.accent : C.warn;
  return (
    <View style={[s.banner, { borderColor: color }]}>
      <Text style={{ color, fontSize: 14 }}>{text}</Text>
    </View>
  );
}

export function ProgressBar({ value, color = C.accent }: { value: number; color?: string }) {
  return (
    <View style={s.track}>
      <View style={[s.fill, { width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`, backgroundColor: color }]} />
    </View>
  );
}

export function StatusPill({ label, status }: { label: string; status: string }) {
  const color =
    status === 'ready'
      ? C.ok
      : status === 'failed'
        ? C.bad
        : status === 'uploading' || status === 'processing' || status === 'queued'
          ? C.accent
          : C.dim;
  return (
    <View style={[s.pill, { borderColor: color }]}>
      <Text style={{ color, fontSize: 12, fontWeight: '600' }}>{label}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  btn: { paddingVertical: 14, paddingHorizontal: 18, borderRadius: 12, alignItems: 'center' },
  btnText: { fontSize: 16, fontWeight: '700' },
  banner: { borderWidth: 1, borderRadius: 10, padding: 12, marginVertical: 6, backgroundColor: 'rgba(255,255,255,0.03)' },
  track: { height: 6, borderRadius: 3, backgroundColor: C.border, overflow: 'hidden' },
  fill: { height: 6, borderRadius: 3 },
  pill: { borderWidth: 1, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 2, alignSelf: 'flex-start' },
});
