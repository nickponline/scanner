# Scanner — Requirements (draft v0.1, 2026-11-07)

Walk around a house with an iPhone, press record, and get back a web viewer with a
Gaussian splat, an extracted mesh, a clean architectural model, a floor plan, and the
camera trajectory with every photo you can look through.

Status: **draft for review**. All open questions resolved, see §11.

---

## 1. Goals and non-goals

### Goals
- G1. Capture a whole home in one continuous walk with an iPhone (video, LiDAR depth, ARKit poses, IMU).
- G2. Upload reliably over flaky home Wi-Fi and queue processing automatically.
- G3. Produce a metric-scale reconstruction: SfM cameras, a dense point cloud, a 3D Gaussian splat, and a mesh.
- G4. Use Gemini 4 Argon in an agent loop to produce (a) a 2D floor plan with labelled rooms and areas and (b) a structured 3D architectural model (walls, floors, ceilings, doors, windows, furniture).
- G5. A web viewer that shows all outputs as toggleable layers, matching the reference UI: layers panel (top left), minimap (bottom left), section-plane slider (right), click-through to source photos.

### Non-goals (v1)
- 360° camera capture (the "360" layer in the reference image). **Insta360** support is planned for a later version; the manifest's `capture_source` field and the camera-model enum leave room for it.
- Android capture.
- Real-time or on-device reconstruction. The phone only captures; all processing runs in the cloud.
- Editing the model in the browser. v1 is a viewer only. Argon re-runs can be triggered from it.
- Measurement-grade (survey) accuracy. Target accuracy is in §9.

---

## 2. System overview

```
iPhone app ──(resumable chunked upload)──► GCS  raw/{scan_id}/
     │                                            │ object finalize
     └── REST: create scan / complete ──► API (Cloud Run) ──► Firestore (scan state)
                                                  │
                                         Cloud Tasks / Pub/Sub queue
                                                  │
                         ┌─────────── Pipeline orchestrator (Cloud Workflows) ───────────┐
                         │ 1 ingest → 2 frames → 3 SfM → 4 depth → 5 splat → 6 LOD      │
                         │          → 7 mesh → 8 Argon plan+model → 9 publish           │
                         └── GPU stages on Cloud Batch (L4 / A100), CPU stages on Cloud Run jobs
                                                  │
                                         GCS  processed/{scan_id}/vNN/
                                                  │
                         Web viewer (static, Cloud Run / Firebase Hosting + CDN)
```

GCP project: **Scanner** (`scanner-514023`), account `nick@liminal.xyz`. Billing is enabled.
Currently enabled APIs: Storage, BigQuery, Datastore, Logging/Monitoring. Still to enable:
Cloud Run, Batch, Workflows, Cloud Tasks/Pub/Sub, Artifact Registry, Firestore, Secret Manager, Generative Language API (+ API key),
Compute (GPU quota: L4 and A100 in one region, see §8).

---

## 3. iOS capture app

### 3.1 Platform
- R3.1 Native Swift / SwiftUI app using ARKit + AVFoundation. Minimum iOS 18.
- R3.2 Requires a LiDAR device. The primary target is the **iPhone 15 Pro / Pro Max**; any LiDAR Pro model is allowed. The app refuses to record without `sceneDepth` support.
- R3.3 Distributed through TestFlight.

### 3.2 Recording
- R3.4 One big **Record** button. Tap to start, tap to stop. Pause/resume within a single scan is supported, and each segment is stored separately.
- R3.5 For each ARKit frame, at ≥30 Hz:
  - RGB: highest ARKit video format available (4K 3840×2160 when `recommendedVideoFormatFor4KResolution` is supported, otherwise 1920×1440), encoded as HEVC at a high bitrate (~50 Mbps at 4K) with **locked exposure duration where possible**, auto white balance locked after 2 s, and no stabilisation.
  - Camera pose (`ARCamera.transform`), intrinsics, tracking state, and exposure/ISO, written per frame to a binary or JSONL sidecar keyed by presentation timestamp.
  - LiDAR `sceneDepth` (256×192 float32) and `confidenceMap`, written losslessly (zstd-compressed raw).
  - IMU (accelerometer, gyro, gravity) at 100 Hz and compass heading for north alignment on the plan.
- R3.6 Additionally capture **high-resolution stills** (`captureHighResolutionFrame`, 12–48 MP) automatically every ~0.75 m of travel or 20° of rotation. These become the "hero photos" for click-through and for SfM and splat training. Capture is paused when `ProcessInfo.thermalState ≥ .serious`, and the interval doubles at `.fair`.
- R3.7 Optional ARKit `ARMeshAnchor`s, saved at stop, used as a coarse geometry prior and for QA.

### 3.3 Live guidance (makes reconstructions succeed)
- R3.8 On-screen coverage feedback: a live top-down minimap of the ARKit mesh so far, with the walked trajectory.
- R3.9 Warnings for moving too fast (motion blur), too-dark scenes, lost tracking, and pointing at blank walls for too long.
- R3.10 Show elapsed time, storage used, and estimated upload size. Hard stop with a warning at configurable limits (default 30 min).

### 3.4 Upload
- R3.11 After stop, the scan is saved locally as a package: `manifest.json` plus segments, sidecars, depth, and stills.
- R3.12 Upload uses a background `URLSession` with **GCS resumable uploads** through signed URLs from the API. It survives app suspension, network changes and relaunch. Upload is allowed only on Wi-Fi by default.
- R3.13 Show per-scan status: Recorded → Uploading (%) → Queued → Processing (stage) → Ready, with push notification when ready.
- R3.14 Local copies are deleted only after the server confirms checksums (CRC32C per object).
- R3.15 Scan list showing name, date, thumbnail and status, and an "Open in viewer" link.

### 3.5 Data contract (`manifest.json`)
Versioned schema covering device model, iOS version, segments, file list with checksums,
intrinsics, depth format, capture settings, and an optional user-entered name/address. Defined in
`schemas/capture_manifest.v1.json` and shared by app and backend.

---

## 4. Backend API and storage

- R4.1 API on Cloud Run (Python FastAPI or Go). Endpoints: `POST /scans`, `POST /scans/{id}/uploads` (signed resumable URLs), `POST /scans/{id}/complete`, `GET /scans`, `GET /scans/{id}`, `POST /scans/{id}/reprocess?from_stage=…`, plus auth.
- R4.2 Auth: Firebase Auth / Identity Platform with Sign in with Apple and Google. Single tenant: an allow-list of you and your team.
- R4.3 Scan state lives in Firestore: status, stage timings, artefact URIs, versions, errors, cost.
- R4.4 GCS layout:
  - `raw/{scan}/…`: immutable capture.
  - `work/{scan}/{stage}/…`: intermediate data, with a 30-day lifecycle.
  - `processed/{scan}/v{NN}/…`: published artefacts.
  - Any stage can be re-run, and that produces a new version (`v04` in the reference UI).
- R4.5 Every stage is idempotent, takes `(scan_id, version, config)` as input, and writes a `stage.json` with inputs, outputs, git SHA, container digest, params, metrics and timing.
- R4.6 Raw captures are retained indefinitely (they are the source of truth for re-processing with better algorithms).

---

## 5. Processing pipeline

Orchestration: **Cloud Workflows** calls one container per stage. GPU stages run as **Cloud Batch**
jobs, which provide L4/A100, spot pricing and retries. CPU stages run as Cloud Run jobs.
Containers live in Artifact Registry and are pinned by digest.

### 5.1 Ingest and validation (CPU)
- Verify checksums and manifest, decode segments, and align video PTS with the pose/depth sidecars.
- Produce a capture QA report: duration, blur stats, tracking-loss intervals, and depth coverage.

### 5.2 Frame selection (CPU/GPU)
- Choose keyframes from video using (a) ARKit-pose baseline/rotation thresholds, (b) a sharpness score (variance of Laplacian), and (c) exposure consistency. Target 1 frame per ~0.25 m or 10°, typically 800–3,000 frames for a house.
- Always include the high-resolution stills (R3.6).
- Output undistorted JPEG/PNG frames, a per-frame ARKit pose prior, and LiDAR depth resampled to frame timestamps.

### 5.3 Structure from Motion: COLMAP 4.2.x
Research summary: COLMAP 4.0 (Mar 2026) merged GLOMAP's **global mapper** into COLMAP. It added
**ALIKED** features with **LightGlue** matching and **position/pose priors**. 4.1 added gravity
priors, and 4.2 (latest 4.2.1, 29 Sep 2026) made the global mapper keep multiple components and
added LoMa learned matching. This is the best maintained, best supported option.

- R5.3.1 Features: ALIKED + LightGlue (ONNX, GPU). SIFT + LightGlue is a configurable fallback. The two feature types are never mixed in one database.
- R5.3.2 Matching: **sequential** matching (window ~20) for video order, plus **retrieval-based loop-closure** matching (vocab tree / learned retrieval) so revisits to the same room close loops. ARKit pose priors limit candidate pairs to spatially plausible ones.
- R5.3.3 Mapping: **global mapper** by default for speed. If the registered-image ratio is below 90%, retry with the incremental mapper.
- R5.3.4 Priors: feed ARKit positions as position priors (std ~0.05–0.2 m, increasing with tracking-loss proximity) and gravity as a gravity prior. Result: metric scale, Z-up gravity alignment, and a frame consistent with ARKit depth.
- R5.3.5 Post: bundle adjustment with shared intrinsics per capture device/format. Sim(3)-align the result to the ARKit trajectory and report RMS residual. Flag segments where ARKit drifted.
- R5.3.6 QA metrics: registered %, mean reprojection error, track length, and number of components. If several components remain, merge them via ARKit priors or fail loudly.
- R5.3.7 Evaluation hook: allow swapping in a learned feed-forward SfM (VGGT/MASt3R-SfM-style) for A/B comparison. Not in the default path.

### 5.4 Depth preparation (GPU)
- Align LiDAR depth to SfM cameras, filter by ARKit confidence (keep "high" and "medium"), and reject depth inconsistent across views (multi-view check).
- Optionally upsample with a monocular depth/normal model guided by LiDAR (prompt-depth style) to give full-resolution depth and normal priors.
- Fuse a **dense point cloud** (for the "Point cloud" layer) with TSDF over filtered depth. This is also the fallback mesh.

### 5.5 Gaussian splat training
Recommendation: **gsplat** (Apache-2.0, nerfstudio) with the **MCMC** densification strategy.
This is the most robust, commercially licensable, fast and memory-efficient implementation, and
MCMC gives a fixed, budgeted Gaussian count.

- R5.5.1 Initialise from the SfM points plus the LiDAR-fused point cloud.
- R5.5.2 Losses: L1 + D-SSIM, **LiDAR depth supervision** with confidence/consistency-filtered weights (AGS-Mesh / DN-Splatter findings show sensor depth beats monocular depth indoors), and normal prior loss.
- R5.5.3 **Per-image appearance embedding / exposure compensation** to handle auto-exposure and windows. Antialiasing (mip-splatting style filter) is on.
- R5.5.4 Budget of 1–3 M Gaussians (configurable), SH degree 3, 30k iterations. Pose refinement is on.
- R5.5.5 Hold out every 8th keyframe; report PSNR/SSIM/LPIPS on it.
- R5.5.6 Output: `splat.ply` (canonical) plus compressed formats (§5.6).

### 5.6 Splat LOD packaging (CPU/GPU)
- Build a streaming LOD hierarchy for **Spark 2.0** (MIT, three.js, World Labs). Use the `.RAD` format with the pre-processed (bhatt-lod) hierarchy, streamed with HTTP Range requests. This matches the reference status bar ("LOD 8 levels / 3,772 nodes").
- Also emit `.spz` / `.sog` for download and compatibility.

### 5.7 Mesh extraction: CoMe
**CoMe: Confidence-Based Mesh Extraction from 3D Gaussians** (Radl et al., Graz/Huawei, ECCV
2026, code released Apr 2026). It reports state-of-the-art unbounded mesh extraction on Tanks &
Temples and ScanNet++ with ~20 min optimisation, about 3× faster than MILo.

- R5.7.1 Run CoMe as a **separate training run** from the same SfM output. It trains its own Gaussians with confidence-weighted losses and extracts the mesh through tetrahedral triangulation (Tetra-NeRF based). Fallback extraction is TSDF.
- R5.7.2 Add LiDAR depth supervision to CoMe. The paper does not use depth priors, so this is our modification behind a flag, evaluated A/B.
- R5.7.3 Post-processing: remove floaters and small components, decimate to ~1–2 M triangles for the viewer plus a 200k proxy, and bake vertex colours or a texture atlas from source images.
- R5.7.4 Output: `mesh.glb` (Draco/Meshopt-compressed) and `mesh.ply`.
- R5.7.5 ⚠️ **Licence:** CoMe's repo ships under the **Inria Gaussian-Splatting licence, which is non-commercial**. MILo, GOF and PGSR are in the same position. Accepted for research/personal use. If this becomes commercial, switch to a gsplat-based mesher with TSDF or marching-tetrahedra extraction, or obtain a licence.
- R5.7.6 Benchmark CoMe against MILo and against TSDF on gsplat depth on 3 reference homes before locking the default.

### 5.8 Floor plan and architectural model: Gemini 4 Argon agent loop
Model: `gemini-4-argon` (GA 13 Oct 2026, 1 M-token output), called through the **Gemini API with a
key created in the Scanner project** and stored in Secret Manager. Use structured output
(JSON schema) and function calling.

**Design principle:** Argon does the reasoning and labelling. Deterministic geometry tools do the
measuring. Argon never invents coordinates it hasn't seen in a tool result.

- R5.8.1 **Pre-computation (deterministic):**
  - Floor and ceiling heights per region come from a z-histogram of points/mesh. v1 assumes a single storey and warns if a second floor level is detected.
  - Orthographic **top-down renders** of the splat and mesh at several cut heights (e.g. 0.1, 1.0, 1.5 m and below ceiling), at 1 cm/px with a metric scale bar and grid.
  - A wall-evidence map: vertical-surface density from mesh normals at 0.5–2.0 m height. Also RANSAC-detected vertical planes and line segments with Manhattan-frame estimation.
  - A per-room-candidate selection of keyframe photos.
- R5.8.2 **Agent tools** exposed to Argon (function calling):
  - `render_topdown(z_min, z_max, bbox, layer)`, `render_view(camera_id | pose)`, `get_photo(id)`
  - `list_planes(filter)`, `fit_wall(segment_ids)`, `snap_polyline(points)`, `measure(p1,p2)`
  - `height_profile(xy)`, `points_in_box(box)`
  - `propose_model(json)`: validates topology (closed rooms, walls connect, doors sit in walls).
  - `render_overlay(model, view)`: draws the current proposal over the top-down image or a photo so Argon can critique it.
- R5.8.3 **Loop:**
  1. Propose the wall graph (polylines, thickness, height).
  2. Segment rooms and label them (kitchen, bedroom…) using photos.
  3. Place doors and windows (position, width, height, swing).
  4. Detect furniture as oriented 3D boxes with class labels.
  5. Self-critique via overlays and fix issues.
  6. Stop when the validator passes and the critique finds no issues, or after N=8 iterations / a token budget. Every step is logged for replay.
- R5.8.4 **Output schema** `building_model.v1.json`:
  - storeys → rooms (polygon, name, area m², floor height, ceiling height)
  - walls (centreline, thickness, height, openings)
  - doors/windows
  - furniture (class, OBB)
  - north angle from compass
  - provenance (tool calls that justify each element)
- R5.8.5 **Derived artefacts:**
  - `plan.svg`: stylised plan like the reference minimap, with room fills, labels, areas, door swings, scale bar and north arrow. Several styles are available ("1 of 12" in the reference).
  - `model_architecture.glb`: walls, floors, ceilings, doors, windows as separate named nodes, solid + edges.
  - `model_furniture.glb`.
  - `ceilings.glb`.
- R5.8.6 **Evaluation:** room-count accuracy, wall IoU against a hand-labelled plan, and area error, on 3–5 reference homes. Target ≤5% total-area error.
- R5.8.7 Human-in-the-loop (v1.1): the user can give a text correction ("the closet is part of the hallway") that is fed back into the loop to produce a new version.

### 5.9 Publish
- Write `processed/{scan}/vNN/scene.json`, the viewer manifest: layer list, URIs, bounds, floor/ceiling heights, camera list, stats. Mark the scan Ready and notify.

### 5.10 Failure handling
- Per-stage retry (×2, with spot preemption retried on on-demand).
- Partial results are still published. For example, if the mesh fails the viewer still shows splat + cameras.
- Errors surface in the app and viewer with the stage name and a human-readable reason.

---

## 6. Web viewer

Stack: TypeScript + Vite + three.js + **Spark 2.0** for splats. React is used for UI chrome only.
Desktop Chrome/Safari/Firefox first, with tablet/mobile best-effort.

### 6.1 Layout (per reference screenshot)
- **Header (top left):** scan name, date, and view-mode toggle `3D · plan` (3D orbit vs. top-down orthographic plan view).
- **Layers panel (top left):**
  - Each row has a visibility toggle, a colour swatch, a summary (version, counts, settings) and a per-layer mode dropdown. `L` hides the panel.
  - Layers:

    | Layer | Content | Modes |
    |---|---|---|
    | Ceilings | `ceilings.glb` | on/off; height range shown |
    | Splat | LOD-streamed 3DGS; shows count and crop height | — |
    | Model — architecture | walls · floors · doors · windows | solid / edges |
    | Model — furniture | labelled oriented furniture boxes | solid / edges |
    | Mesh | CoMe mesh | colour / normals / wire |
    | Point cloud | fused dense cloud | back faces hidden toggle |
    | Photos | camera frusta / markers for keyframes | keyframes / hero only / all |
    | Trajectory | walked path polyline | — |
    | Plan | `plan.svg` texture on the floor plane; style picker | — |
    | Ground | grid | lines / checker; cell size; fade distance |

  - **"··· engineering"** expander: splat budget, LOD bias, point size, SH degree, draw mode, and a stats overlay.
- **Minimap (bottom left):** plan SVG with room labels and areas, photo/station markers, a live "you are here" marker with view cone, a north arrow and a scale bar. Click to fly there; drag the cone to rotate.
- **Section slider (right):**
  - A vertical slider showing floor (0) and ceiling markers. The handle sets a horizontal clipping plane height, labelled (e.g. "2.47 · splat").
  - `cut · off` toggle. The clip applies to splat, mesh, model and point cloud together, with an option to clip each separately.
  - Snaps to floor, ceiling and default 1.2 m.
- **Status bar (bottom centre):** active layer stats, e.g. splat count, SH degree, LOD levels/nodes, and load time.

### 6.2 Photo click-through
- R6.1 Clicking a photo marker (3D or minimap) animates the camera to that exact SfM pose, intrinsics and FOV. The original photo is shown full-frame and aligned with the 3D, with a cross-fade slider (photo ↔ render).
- R6.2 Previous/next along the trajectory with arrow keys. Esc returns to orbit.
- R6.3 The photo loads full-resolution progressively, from a thumbnail first.

### 6.3 Navigation and performance
- Orbit / pan / zoom, plus first-person walk mode (WASD) at eye height. Double-click to fly to a surface point.
- First splat render in under 3 s on a home connection (LOD streaming). 60 fps on an M-series MacBook with 1 M splats visible.
- Deep links: `/{scan}?v=04&layers=…&cam=…&cut=2.47` reproduces the view.

### 6.4 Hosting and access
- Static bundle on Firebase Hosting or Cloud Run behind Cloud CDN. Artefacts are served from GCS via signed URLs or a CDN with auth cookie.
- Private by default. Optional unlisted, revocable share links.

---

## 7. Repository layout (`scanner`)

```
ios/              Swift app (Xcode project, SwiftPM)
api/              Cloud Run API
pipeline/
  common/         stage runner, manifest, GCS IO, stage.json
  stages/         ingest, frames, sfm, depth, splat, lod, mesh, argon, publish
  docker/         one image per heavy stage (colmap, gsplat, come, argon)
  workflows/      Cloud Workflows YAML
viewer/           TS/three.js/Spark web app
schemas/          capture_manifest, scene, building_model (JSON Schema, shared)
infra/            Terraform for the GCP project
eval/             reference scans, metrics, regression dashboard
docs/
```

---

## 8. Infrastructure and cost

- Terraform for all GCP resources. One region with L4 and A100 availability (e.g. `us-central1`). GPU quota request: 4× L4 and 2× A100-80GB to start.
- Compute per stage (estimates for a ~150 m² home, ~2,000 frames):

  | Stage | Hardware | Time |
  |---|---|---|
  | SfM | 1× L4, 16 vCPU | 15–40 min |
  | Splat | 1× L4 (24 GB) or A100 | 20–40 min |
  | CoMe | 1× A100 (memory headroom for tetra extraction) | 30–60 min |
  | Argon loop | — | 5–20 min |

- Target turnaround: **≤ 2 h** from upload complete to Ready. Quality is prioritised over cost: use A100s where they help, with a budget of **≤ $30/scan** including Argon tokens.
- Observability: Cloud Logging, a per-stage metrics table in BigQuery, and alerts on failures.

## 9. Quality targets
- Registered frames ≥ 95%. Metric scale error ≤ 1% (checked with a tape-measured reference).
- Splat hold-out PSNR ≥ 27 dB indoors.
- Floor plan wall position error ≤ 5 cm median. Room area error ≤ 5%.
- End-to-end success without manual intervention on ≥ 90% of guided captures.

## 10. Milestones
1. **M0 — Skeleton:** Terraform, API, upload, a stub pipeline that publishes the trajectory, and a viewer that shows trajectory + photos.
2. **M1 — Capture app:** recording with all sensors, resumable upload, status.
3. **M2 — SfM + splat:** COLMAP 4.2 with priors, gsplat MCMC with depth, Spark LOD in the viewer, section slider, photo click-through.
4. **M3 — Mesh:** CoMe (or the licence-safe alternative), mesh layer.
5. **M4 — Argon:** floor plan + architectural/furniture model, minimap, plan view.
6. **M5 — Polish and eval:** reference-home benchmark, cost and latency tuning, sharing.

## 11. Open questions
Resolved (2026-11-07):
- **Q1. Licence:** Research/personal. CoMe is the default mesher; revisit if this becomes commercial.
- **Q2. Building scope:** Single floor, up to ~250 m², for v1. Multi-storey comes later; keep `storeys[]` in the schema.
- **Q3. Users:** Just you / your team, private behind sign-in, with optional unlisted share links.
- **Q5. Budget:** Quality first. Use A100s and longer training; ~1–2 h and ~$15–30 per scan is acceptable.

- **Q4. Devices:** The target device is the iPhone 15 Pro / Pro Max (the base iPhone 15 has no LiDAR). LiDAR is required; non-LiDAR devices are blocked rather than degraded. Distribution is via TestFlight.
- **Q6. Photos:** High-res stills are on (my call, see R3.6). They are throttled automatically when the device reports a serious thermal state.
- **Q7. Argon access:** Gemini API key created in the Scanner project, stored in Secret Manager, and used by the Argon stage only.
- **Q8.** Confirmed: the mesher is **CoMe**.
- **Q9.** No 360 for now. Insta360 support is planned for the future; keep a `capture_source` field in the manifest and camera model in the schema so equirectangular stations can be added.
- **Q10.** Furniture is represented as labelled oriented boxes (no furniture meshes).

No open questions remain.

## 12. References
- COLMAP 4.0 (global mapper, ALIKED/LightGlue, priors): https://radiancefields.com/colmap-4.0-introduces-major-performance-and-infrastructure-updates
- COLMAP 4.2 (multi-component global mapping, LoMa): https://radiancefields.com/colmap-4.2.0-adds-multi-component-global-mapping-and-loma-learned-matching
- gsplat: https://github.com/nerfstudio-project/gsplat · 3DGS-MCMC: https://arxiv.org/abs/2404.09591
- AGS-Mesh (smartphone LiDAR priors indoors): https://arxiv.org/abs/2411.19271 · DN-Splatter: https://maturk.github.io/dn-splatter
- CoMe: https://r4dl.github.io/CoMe · https://github.com/r4dl/CoMe · https://arxiv.org/abs/2603.24725
- MILo: https://github.com/Anttwo/MILo
- Spark 2.0: https://www.worldlabs.ai/blog/spark-2.0 · https://github.com/sparkjsdev/spark
- Gemini 4 Argon: https://ai.google.dev/gemini-api/docs/models/gemini-4-argon
