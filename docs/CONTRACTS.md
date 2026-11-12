# Scanner — interface contracts (v1)

Single source of truth for the formats shared between the **app** (`mobile/`), **API** (`api/`),
**pipeline** (`pipeline/`) and **viewer** (`viewer/`). JSON Schemas live in `schemas/`.
If you change something here, update every consumer.

---

## 1. Coordinate frames

| Frame | Convention |
|---|---|
| **ARKit raw** (capture) | Right-handed, **Y up**, gravity-aligned, metres. `ARCamera.transform` = camera→world, camera looks down **−Z**, +X right, +Y up (OpenGL camera). Stored exactly as ARKit gives it. |
| **World** (all processed outputs) | Right-handed, **Z up**, metres, gravity-aligned. **Floor at z = 0.** XY origin at the centre of the floor bounding box. +X is aligned to the dominant wall direction (Manhattan frame). |
| **Camera** (processed outputs) | **OpenCV**: +X right, +Y down, +Z forward. `c2w` is a 4×4 **row-major** camera→world matrix in the World frame. Intrinsics `fx, fy, cx, cy` are in pixels of the image as served (undistorted). |
| **Viewer** | three.js is Y-up: the viewer puts everything under one root `Object3D` rotated −90° about X (world Z → three Y). |

`north_deg`: the compass bearing of World **+Y**, in degrees clockwise from true north (0 = +Y points north).

---

## 2. Capture package (phone → cloud)

A directory per scan, `scan_<ulid>/`, which is uploaded file-by-file as is.

```
manifest.json                  # schemas/capture_manifest.v1.json
video/seg_000.mov              # HEVC, ARKit capturedImage, native orientation (landscape sensor), one file per segment
frames/seg_000.jsonl           # one JSON line per frame written to the video, in order
depth/seg_000.bin              # LiDAR depth + confidence records (format below)
stills/000000.jpg …            # high-res stills (captureHighResolutionFrame), EXIF orientation = sensor
stills/stills.jsonl            # one line per still
imu/seg_000.csv                # t,ax,ay,az,gx,gy,gz,grav_x,grav_y,grav_z,heading_deg (100 Hz)
arkit_mesh.ply                 # optional: ARMeshAnchors merged in ARKit world frame at stop (binary PLY)
```

### frames/seg_NNN.jsonl (one line per video frame, line i ⇔ video frame i)
```json
{"i":0,"t":12345.678901,"pts":0.0,"T":[16 floats, column-major camera→world (simd_float4x4 order)],
 "K":[fx,fy,cx,cy],"w":1920,"h":1440,"track":"normal|limited:<reason>|notAvailable",
 "exp":0.008333,"iso":400.0,"depth":true}
```
- `t` = ARFrame.timestamp (seconds, system uptime clock); `pts` = presentation time in the .mov (s).
- `K` is in pixels of the captured image (`w`×`h`), sensor orientation.
- `depth`: true if a depth record for this frame exists in depth/seg_NNN.bin.

### depth/seg_NNN.bin
Concatenated records, little-endian:
```
magic  "SDP1" (4 bytes)
i      uint32  frame index (matches frames jsonl "i")
t      float64 ARFrame timestamp
w,h    uint16, uint16       (256×192 typical)
n      uint32  byte length of payload
payload: zlib-compressed bytes of [ depth float32 metres w*h ][ confidence uint8 w*h (0 low,1 med,2 high) ]
```
Depth is written for every 3rd frame (~10–20 Hz). Depth intrinsics = `K` scaled by `w_depth/w`.

### imu/seg_NNN.csv
Header row then one row per `CMDeviceMotion` sample (100 Hz, device frame, CoreMotion axes): `t` = sample
timestamp (same uptime clock as ARFrame `t`); `ax,ay,az` = total specific force (userAcceleration + gravity)
in **g**; `gx,gy,gz` = rotation rate in **rad/s**; `grav_*` = gravity vector in **g**; `heading_deg` =
`CMDeviceMotion.heading` (magnetic, −1 when unavailable). *(Units clarified by mobile, 2026-11-07.)*

### stills/stills.jsonl
```json
{"file":"stills/000000.jpg","t":12346.1,"T":[16 col-major],"K":[fx,fy,cx,cy],"w":4032,"h":3024,"seg":0}
```

### manifest.json (abridged; see schema)
```json
{"schema":"capture_manifest.v1","scan_id":"scan_01J…","name":"Apartment","created_at":"ISO8601",
 "capture_source":"iphone_arkit","device":{"model":"iPhone16,1","os":"iOS 26.1","app_version":"0.1.0"},
 "video_format":{"w":1920,"h":1440,"fps":30,"codec":"hevc"},
 "segments":[{"index":0,"video":"video/seg_000.mov","frames":"frames/seg_000.jsonl","depth":"depth/seg_000.bin",
              "imu":"imu/seg_000.csv","n_frames":5400,"duration_s":180.0}],
 "stills":"stills/stills.jsonl","arkit_mesh":"arkit_mesh.ply",
 "files":[{"path":"video/seg_000.mov","size":123,"crc32c":"base64"}],
 "heading_deg_at_start":123.4}
```
- `files[]` lists every package file except `manifest.json` itself (a file can't carry its own CRC);
  `manifest.json` is still uploaded if the API returns an upload URL for it.
- `heading_deg_at_start`: compass bearing (degrees clockwise from **magnetic** north) of the **ARKit world −Z
  axis**, estimated on the phone from CoreMotion attitude + ARKit camera yaw. Omitted when no magnetometer
  fix. *(Clarified by mobile, 2026-11-07; matches how pipeline/depth uses it.)*
- `stills` / `arkit_mesh` are omitted when no stills / no mesh were captured.

---

## 3. HTTP API (`api/`, Cloud Run)

Base: `https://<api-host>/v1`. Auth: `Authorization: Bearer <token>` (tokens are allow-listed in
Secret Manager `api-tokens`). The viewer may instead use `?share=<share_token>` (read-only, one scan).

| Method & path | Body → Response |
|---|---|
| `POST /scans` | `{manifest}` → `{scan_id, uploads:[{path, url}]}`. `url` is a **GCS resumable session URI** per file. The client `PUT`s bytes (optionally in `Content-Range` chunks) and queries the offset with `PUT` + `Content-Range: bytes */<size>` to resume. |
| `POST /scans/{id}/uploads` | `{paths:[…]}` → fresh session URIs, used if a session expired (sessions last 7 days). |
| `POST /scans/{id}/complete` | → `{status:"queued"}`. Verifies all files exist with matching size/CRC32C, then starts the pipeline. |
| `GET /scans` | → `[{scan_id,name,created_at,status,stage,progress,thumbnail_url,viewer_url}]` |
| `GET /scans/{id}` | → full status: `{…, stages:{name:{status,started_at,ended_at,metrics,error}}, versions:[…]}` |
| `POST /scans/{id}/reprocess` | `{from_stage, config?}` → new version number. |
| `GET /scans/{id}/scene?v=N` | (bearer or `?share=`) → `scene.json` with every `url` absolute, of the form `https://<host>/v1/f/<access_token>/<scan_id>/v<NN>/<path>` (302 → signed GCS URL), plus top-level `base_url` = the `…/v<NN>/` prefix. |
| `POST /scans/{id}/share` | → `{share_url}` |
| `POST /devices/push-token` | `{expo_push_token}` → 204, used for push notification when a scan is Ready. |

`status`: `created → uploading → queued → processing → ready | failed`.
`stage` ∈ `ingest, frames, sfm, depth, splat, lod, mesh, plan, publish`.

---

## 4. Processed outputs: `gs://<bucket>/processed/{scan_id}/v{NN}/`

```
scene.json                    # schemas/scene.v1.json: the viewer entry point
cameras.json                  # photos/keyframes with poses
trajectory.json               # walked path
photos/full/{id}.jpg          # undistorted, ≤ 2048 px long edge
photos/thumb/{id}.jpg         # 256 px
splat/scene.rad               # Spark LOD (streamed via HTTP Range)
splat/scene.spz               # compact fallback / download
splat/scene.ply               # canonical 3DGS ply (download only)
mesh/mesh.glb                 # CoMe mesh, vertex colours, meshopt-compressed
points/points.ply             # fused point cloud, binary little-endian, x y z float + red green blue uchar (+ nx ny nz)
model/building_model.json     # schemas/building_model.v1.json
model/architecture.glb        # walls/floors/doors/windows: one named node per element, name = element id
model/furniture.glb           # one node per furniture box, extras.label
model/ceilings.glb
plan/plan_{style}.svg         # stylised floor plans
```

### scene.json
```json
{"schema":"scene.v1","scan_id":"…","version":4,"name":"Apartment","captured_at":"…","created_at":"…",
 "units":"m","up":"z","north_deg":12.0,
 "bounds":{"min":[-4,-6,-0.1],"max":[4,6,3.1]},
 "levels":{"floor":0.0,"ceiling":2.52,"ceiling_max":3.06},
 "default_view":{"position":[8,-10,9],"target":[0,0,1]},
 "layers":[
  {"id":"ceilings","kind":"model","label":"Ceilings","url":"model/ceilings.glb","visible":false,"summary":"v04 · 2.52–3.06 m"},
  {"id":"splat","kind":"splat","label":"Splat","url":"splat/scene.rad","fallback_url":"splat/scene.spz","visible":true,
   "summary":"iPhone · 3DGS · 961 k","stats":{"count":961367,"sh_degree":3,"lod_levels":8,"lod_nodes":3772}},
  {"id":"architecture","kind":"model","label":"Model","url":"model/architecture.glb","visible":true,"summary":"v04 · walls · floors · doors · windows","modes":["solid","edges"],"mode":"edges"},
  {"id":"furniture","kind":"model","label":"Model","url":"model/furniture.glb","visible":true,"summary":"v04 · furniture","modes":["solid","edges"],"mode":"edges"},
  {"id":"mesh","kind":"mesh","label":"Mesh","url":"mesh/mesh.glb","visible":false,"summary":"CoMe · 1.2 M tris","modes":["color","normals","wire"],"mode":"color"},
  {"id":"pointcloud","kind":"points","label":"Point cloud","url":"points/points.ply","visible":false,"summary":"4.1 M · back faces hidden"},
  {"id":"photos","kind":"cameras","label":"Photos","url":"cameras.json","visible":true,"summary":"336 photos","modes":["keyframes","hero","all"],"mode":"keyframes"},
  {"id":"trajectory","kind":"trajectory","label":"Trajectory","url":"trajectory.json","visible":false},
  {"id":"plan","kind":"plan","label":"Plan","visible":true,"summary":"Stylized plan",
   "styles":[{"id":"stylized","url":"plan/plan_stylized.svg"},{"id":"technical","url":"plan/plan_technical.svg"},{"id":"mono","url":"plan/plan_mono.svg"}],"style":"stylized"},
  {"id":"ground","kind":"ground","label":"Ground","visible":true,"cell":0.5,"fade":4.0,"modes":["lines","checker"],"mode":"lines"}],
 "building_model_url":"model/building_model.json",
 "stats":{"processing_s":4210,"frames":2140,"registered":2131}}
```
A layer whose stage failed is **omitted** from `layers`; the viewer must cope with any subset.

**GLB frame:** every GLB (`mesh.glb`, `architecture.glb`, `furniture.glb`, `ceilings.glb`) stores raw **World**
coordinates (Z up, metres), *not* the usual glTF Y-up convention. The viewer adds them under the same Z-up→Y-up
root as the splat and point cloud, with no extra rotation. GLBs may use `EXT_meshopt_compression` /
`KHR_mesh_quantization` (gltfpack `-cc -kn -km`); node names (element ids) must survive packing.

**Relative URLs** in any file (scene.json, cameras.json `full`/`thumb`, …) are resolved by the viewer against the
URL of the file that contains them (`new URL(rel, fileUrl)`), never against the page. The API serves assets at
`https://<host>/v1/f/<access_token>/<scan_id>/v<NN>/<path>` (token in the path, 302 → short-lived signed GCS URL), so
relative references keep working. The scene response also carries `"base_url"` (the `…/v<NN>/` prefix), which the viewer
uses as the base for relative URLs in scene.json when present. Only `/v1/scans*` calls take `Authorization: Bearer`;
`/v1/f/…` URLs need no headers. Fixture mode (`?scene=…/scene.json`) resolves against the scene.json location.

### cameras.json
```json
{"cameras":[{"id":"kf_000123","kind":"keyframe|hero","t":12.34,"full":"photos/full/kf_000123.jpg","thumb":"photos/thumb/kf_000123.jpg",
  "w":2048,"h":1536,"fx":1600.1,"fy":1600.1,"cx":1024,"cy":768,"c2w":[16 floats row-major, OpenCV cam, World frame],"room_id":"r3"}]}
```

### trajectory.json
`{"t":[…],"p":[[x,y,z],…]}`: World-frame camera centres at ~10 Hz.

### Plan SVG mapping
Root `<svg>` carries `data-world-bounds="minx miny maxx maxy"` and `data-px-per-m="100"`.
World (x, y) → SVG (u, v) = `((x − minx)·s, (maxy − y)·s)`. Rooms are `<g class="room" data-room-id>`,
and camera positions are **not** baked in (the viewer overlays them).

### building_model.json (see `schemas/building_model.v1.json`)
```json
{"schema":"building_model.v1","units":"m","up":"z","north_deg":12.0,
 "storeys":[{"id":"s0","name":"Ground","floor_z":0.0,"ceiling_z":2.52,
  "rooms":[{"id":"r1","name":"Kitchen","type":"kitchen","polygon":[[x,y],…],"area_m2":12.4,"ceiling_z":2.6}],
  "walls":[{"id":"w1","start":[x,y],"end":[x,y],"thickness":0.12,"height":2.52,"base_z":0.0,"exterior":true}],
  "openings":[{"id":"d1","type":"door|window|opening","wall_id":"w1","center":[x,y],"width":0.82,"height":2.03,"sill_z":0.0,
               "swing":{"hinge":"start|end","side":"left|right"}}],
  "furniture":[{"id":"f1","label":"sofa","center":[x,y,z],"size":[sx,sy,sz],"yaw_deg":90,"room_id":"r2"}]}],
 "provenance":{"model":"gemini-4-argon","iterations":5,"created_at":"…","notes":"…"}}
```
