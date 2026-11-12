import { requireNativeView } from 'expo';
import * as React from 'react';
import { Text, View } from 'react-native';

import type { CapturePreviewProps } from './ScannerCapture.types';
import ScannerCapture from './ScannerCaptureModule';

const NativeView: React.ComponentType<CapturePreviewProps> | null = ScannerCapture
  ? requireNativeView('ScannerCapture')
  : null;

export default function CapturePreview(props: CapturePreviewProps) {
  if (!NativeView) {
    return (
      <View style={[{ backgroundColor: '#111', alignItems: 'center', justifyContent: 'center' }, props.style]}>
        <Text style={{ color: '#888' }}>Native capture module not linked</Text>
      </View>
    );
  }
  return <NativeView {...props} />;
}
