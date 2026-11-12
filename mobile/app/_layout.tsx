import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { C } from '../src/components/theme';
import { AppProvider } from '../src/state/AppContext';

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <AppProvider>
        <StatusBar style="light" />
        <Stack
          screenOptions={{
            headerStyle: { backgroundColor: C.bg },
            headerTintColor: C.text,
            contentStyle: { backgroundColor: C.bg },
          }}
        >
          <Stack.Screen name="index" options={{ title: 'Scans' }} />
          <Stack.Screen name="capture" options={{ headerShown: false, gestureEnabled: false }} />
          <Stack.Screen name="scan/[id]" options={{ title: 'Scan' }} />
          <Stack.Screen name="settings" options={{ title: 'Settings' }} />
        </Stack>
      </AppProvider>
    </SafeAreaProvider>
  );
}
