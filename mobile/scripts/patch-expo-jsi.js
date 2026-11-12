/**
 * Expo SDK 58's expo-modules-jsi is compiled from source (Swift 6 language mode) and was written against
 * a newer Swift than Xcode 26.3's 6.2.4, which rejects one region-isolation check:
 *   JavaScriptObject.swift:946 "sending 'slot' risks causing data races"
 * The closure runs synchronously on the same thread (see the comment in that file), so we pass the
 * pointer across as a Sendable integer instead. Idempotent; a no-op once Expo changes the code.
 * Delete this script (and the postinstall hook) when building with Xcode 26.4+.
 */
const fs = require('fs');
const path = require('path');

let file;
try {
  file = path.join(
    path.dirname(require.resolve('expo-modules-jsi/package.json')),
    'apple/Sources/ExpoModulesJSI/Runtime/Values/JavaScriptObject.swift',
  );
} catch {
  process.exit(0);
}
if (!fs.existsSync(file)) process.exit(0);
const src = fs.readFileSync(file, 'utf8');
const from = `    try runtime._withUnsafeGuaranteedRef { runtime in
      try JavaScriptActor.assumeIsolated {
        try encode(JavaScriptValue.Slot(slot), runtime)
      }
    }`;
const to = `    // patched by mobile/scripts/patch-expo-jsi.js (Swift 6.2.4 region-isolation false positive)
    let slotAddress = UInt(bitPattern: slot)
    try runtime._withUnsafeGuaranteedRef { runtime in
      try JavaScriptActor.assumeIsolated {
        try encode(JavaScriptValue.Slot(UnsafeMutablePointer<facebook.jsi.Value>(bitPattern: slotAddress)!), runtime)
      }
    }`;
if (src.includes(from)) {
  fs.writeFileSync(file, src.replace(from, to));
  console.log('[patch-expo-jsi] patched JavaScriptObject.swift for Xcode 26.3');
}
