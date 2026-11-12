# Scanner — iPhone capture app (`mobile/`)

Expo (SDK 58, React Native 0.88, expo-router) app with a local Swift native module
(`modules/scanner-capture`) that does all ARKit capture, file writing, checksums and background upload.
Formats and API are defined in [`../docs/CONTRACTS.md`](../docs/CONTRACTS.md) §2–3.

Target: **iPhone 15 Pro / Pro Max** (any LiDAR Pro model works), iOS 18+. Non-LiDAR devices and the
simulator show a "LiDAR iPhone (Pro) required" screen on the capture route; the scan list and settings still work.

```
app/                         expo-router screens
  index.tsx                  scan list (local packages merged with GET /v1/scans)
  capture.tsx                record / pause / resume, warnings, minimap, timers, storage
  scan/[id].tsx              upload progress, server stage polling, "Open in viewer"
  settings.tsx               API URL, token (Keychain), Wi-Fi only, max length, mesh overlay
src/lib/
  api.ts                     HTTP client for /v1
  orchestrator.ts            POST /scans → native upload → POST /complete → delete local (jest-tested)
  scans.ts                   list merge + formatting helpers
  settings.ts push.ts network.ts
src/state/AppContext.tsx     wiring: native events, AppState/network resume, push registration
modules/scanner-capture/
  ios/Core/                  pure Swift (Foundation + zlib): CRC32C, DepthRecord (SDP1), Manifest, PLY, ULID, sidecar lines
  ios/CaptureEngine.swift    ARSession, recording state machine, stills, status/map events
  ios/SegmentWriter.swift    HEVC AVAssetWriter + frames jsonl + depth bin + IMU csv per segment
  ios/UploadManager.swift    background URLSession → GCS resumable sessions, persisted state, resume
  ios/CapturePreviewView.swift  ARSCNView preview + ARMeshAnchor wireframe overlay
  ios/ScannerCaptureAppDelegate.swift  handleEventsForBackgroundURLSession
  tests/                     standalone macOS test: emits a package, Python decodes + verifies it
scripts/patch-expo-jsi.js    postinstall: Xcode 26.3 workaround for expo-modules-jsi (see file)
```

## Prerequisites

- macOS with **Xcode 26.x** and CocoaPods (`brew install cocoapods`).
- **Node ≥ 22.13** (Expo CLI refuses older). `package.json` pins Node 22.19 for Volta users.
- An Apple Developer account (paid team recommended: push notifications and TestFlight need it).

```bash
cd mobile
npm install
```

## Build a development build onto your iPhone 15 Pro

1. Plug in the iPhone (or pair over Wi-Fi in Xcode → Devices), unlock it, trust the Mac, and enable
   **Developer Mode** (Settings → Privacy & Security → Developer Mode).
2. Generate the native project: `npx expo prebuild -p ios` (re-run after changing `app.json`, plugins
   or native module config; `ios/` is generated and git-ignored).
3. Set your signing team **once**:
   - `open ios/Scanner.xcworkspace` → target **Scanner** → *Signing & Capabilities* → tick
     *Automatically manage signing* → pick your **Team**. Bundle id is `xyz.liminal.scanner`
     (change it there and in `app.json` if it's taken in your team).
   - The app has the **Push Notifications** capability (from `expo-notifications`). A free personal
     team can't sign that; either use a paid team or remove the `expo-notifications` plugin entry from
     `app.json` and re-run prebuild (push is best-effort anyway).
   - `npx expo prebuild --clean` regenerates `ios/` and resets the team; to make it stick, set
     `"ios": { "appleTeamId": "<YOUR_TEAM_ID>" }` in `app.json`.
4. Build + install + start Metro:
   ```bash
   npx expo run:ios --device            # pick the iPhone from the list
   # Release build (no Metro, realistic performance):
   npx expo run:ios --device --configuration Release
   ```
   For a Debug build the app opens the dev-client launcher; it connects to Metro on your Mac (same Wi-Fi).
   Use a **Release** build for real capture tests: Debug Swift is much slower (CRC32C, zlib).

### Alternative: EAS Build → TestFlight

```bash
npm i -g eas-cli
eas login
eas init                       # creates the EAS project and writes extra.eas.projectId into app.json
eas build -p ios --profile development   # internal dev-client build (register device with `eas device:create`)
eas build -p ios --profile production    # store build
eas submit -p ios --latest               # upload to App Store Connect → TestFlight
```
`eas init` also gives the app an EAS `projectId`, which `expo-notifications` needs to mint Expo push
tokens. Without it the app skips push registration (logged, not an error).

## Configure the API

Settings tab (top right of the scan list):

- **API base URL** — e.g. `https://scanner-api-xxxx-uc.a.run.app` (the app appends `/v1` if missing).
- **API token** — one of the tokens in Secret Manager `api-tokens`. Stored in the iOS Keychain
  (`expo-secure-store`, after-first-unlock, this device only), sent as `Authorization: Bearer …`.
- **Upload on Wi-Fi only** (default on) — enforced natively per request (`allowsCellularAccess`,
  `allowsExpensiveNetworkAccess`); changing it restarts in-flight uploads under the new policy.
- *Save & test connection* calls `GET /v1/scans`.

## How capture and upload work

- AR session: `ARWorldTrackingConfiguration`, `.sceneDepth`, `.mesh` reconstruction, gravity alignment,
  4K format when `recommendedVideoFormatFor4KResolution` exists (else largest ≥30 fps), video HDR off,
  max exposure capped at 1/60 s, white balance locked 2 s after record starts.
- Each frame written to the HEVC `.mov` (no B-frames, 1 s GOP, ~50 Mbps at 4K) gets one line in
  `frames/seg_NNN.jsonl`; every 3rd written frame also gets an SDP1 depth record (zlib, Python-decodable).
  Frames the encoder can't take are dropped from *both* so line i ⇔ video frame i always holds.
- IMU 100 Hz via CoreMotion; high-res stills via `captureHighResolutionFrame` every 0.75 m / 20°
  (doubled at thermal `.fair`, paused at `.serious`+). ARMeshAnchors → `arkit_mesh.ply` at stop.
- Pause/resume closes and opens segments. Stop writes `manifest.json` with sizes + CRC32C (GCS format).
- Packages live in `Documents/scans/scan_<ulid>/` (visible in Finder → iPhone → Files → Scanner for
  pulling raw captures by cable), excluded from iCloud backup.
- Upload: `POST /v1/scans` → native background `URLSession` PUTs each file to its GCS resumable
  session (files > 64 MiB in 64 MiB `Content-Range` chunks from temp slices). On failure / relaunch it
  queries the committed offset (`Content-Range: bytes */SIZE` → 308 `Range`) and sends only the rest.
  Expired sessions (404/410) → `POST /v1/scans/{id}/uploads`. When all files are done →
  `POST /v1/scans/{id}/complete`; local files are deleted **only** after that succeeds.

## Tests

```bash
npm run typecheck     # tsc --noEmit
npm run lint          # expo lint (eslint-config-expo)
npm test              # jest: orchestrator (happy path, resume after kill, expired URIs, 409, retries), api, list merge
npm run test:native   # swiftc the pure Swift core on macOS, emit a package, decode it in python3 -I
```
Simulator smoke test (ARKit doesn't run there; the capture route shows the unsupported screen):
```bash
npx expo prebuild -p ios
xcodebuild -workspace ios/Scanner.xcworkspace -scheme Scanner -configuration Release \
  -sdk iphonesimulator -destination 'platform=iOS Simulator,name=iPhone 16 Pro' \
  -derivedDataPath ios/build/dd CODE_SIGNING_ALLOWED=NO build
xcrun simctl install booted ios/build/dd/Build/Products/Release-iphonesimulator/Scanner.app
xcrun simctl launch booted xyz.liminal.scanner
# open a route directly (avoids the "Open in Scanner?" prompt of `simctl openurl`):
SIMCTL_CHILD_SCANNER_INITIAL_ROUTE=/capture xcrun simctl launch booted xyz.liminal.scanner   # or /settings
```

## Field-test checklist (iPhone 15 Pro)

Setup
- [ ] Release build installed; Settings → API URL + token saved; *Save & test connection* succeeds.
- [ ] Camera + Motion permission prompts appear on first capture; notification prompt appears once.
- [ ] Settings → Device shows `iPhone16,1`, LiDAR depth **yes**, mesh **yes**, video **3840×2160 @ 30**.

Capture (walk 2–3 rooms, ~5 min)
- [ ] Preview is live, mesh wireframe grows; minimap shows coverage cells + trajectory + heading cone.
- [ ] fps ≈ 30, tracking "normal", dropped frames stays ~0 (status line); photos counter rises while walking.
- [ ] Warnings fire: shake/spin fast → "Slow down"; lights off → "Too dark"; face a blank wall
      for 3 s → "Blank surface"; hold < 40 cm from a wall → "Too close".
- [ ] Pause → resume → stop: scan has 2 segments (`seg_000`, `seg_001`).
- [ ] Receive a phone call / lock screen mid-recording: recording pauses (interrupted), resume works.
- [ ] Phone gets warm: thermal warning appears and photos pause at "serious".
- [ ] Set max length to 1 min in Settings: recording hard-stops with an alert.
- [ ] Stop shows "Finalising…" then the scan detail; size/duration look right.

Package sanity (Finder → iPhone → Files → Scanner → `scans/scan_…`, copy to Mac)
- [ ] `video/seg_000.mov` plays (rotated: landscape sensor orientation is expected); frame count
      equals the line count of `frames/seg_000.jsonl` (`ffprobe -count_frames`).
- [ ] `python3 -I mobile/modules/scanner-capture/tests/verify.py` style decode of `depth/*.bin` works
      (or run the pipeline ingest stage on it locally); `stills/*.jpg` are 4K+ and sharp.
- [ ] `manifest.json` CRC32C matches `gsutil hash -c` of the uploaded objects.

Upload
- [ ] On Wi-Fi: upload starts automatically after stop; progress % rises in list and detail.
- [ ] Wi-Fi only on + switch to cellular: uploads wait ("Waiting for Wi-Fi"), resume on Wi-Fi.
- [ ] Kill the app (swipe up) mid-upload of a large video; wait a minute; relaunch → progress continues
      from where GCS left off (not from 0), and finishes.
- [ ] Airplane mode mid-upload → off: resumes without re-sending committed bytes.
- [ ] After `/complete`: status moves to Queued → Processing (stage) → Ready; local package is gone.
- [ ] Push notification arrives when Ready (needs EAS projectId + paid team); "Open in viewer" opens the viewer.

## Known limitations / notes

- Nothing ARKit-related can run in the simulator; capture, stills, mesh and background upload are
  verified only by the field test above.
- `heading_deg_at_start` is relative to **magnetic** north (no location permission requested).
- `scripts/patch-expo-jsi.js` (npm `postinstall`) rewrites one line in `expo-modules-jsi`
  (JavaScriptObject.swift) that Xcode 26.3's Swift 6.2.4 rejects as a data race; Expo SDK 58 targets a
  newer Swift. Remove it when building with Xcode 26.4+. Re-run `npm install` (or the script) after
  wiping `node_modules`.
- Thumbnails are local-only (`Application Support/Scanner/meta/<id>/thumb.jpg`); after upload the list
  uses the server's `thumbnail_url`.
