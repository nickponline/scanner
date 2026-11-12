"""Stage 9 — assemble processed/{scan}/vNN/ for the viewer (CONTRACTS.md §4) and mark the scan ready.

Any layer whose stage failed/was skipped is simply omitted from scene.json.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import cv2
import numpy as np

from ..capture import read_jsonl
from ..ctx import StageContext, now_iso
from ..geom import arkit_to_zup_cv

log = logging.getLogger("scanner.publish")

# Gaussian splat is disabled for now: no splat layer is published even if a splat/lod stage exists for the version.
SPLAT_ENABLED = False


def upright_k(c2w: np.ndarray) -> int:
    """Number of 90° CCW image rotations (np.rot90) that make world-up point image-up."""
    up = c2w[:3, :3].T @ np.array([0.0, 0.0, 1.0])
    best, best_k = -9.0, 0
    x, y = up[0], up[1]
    for k in range(4):
        if -y > best:
            best, best_k = -y, k
        x, y = y, -x  # one CCW rotation: x' = y, y' = -x
    return best_k


def rotate_camera(K, c2w, W, H, k):
    fx, fy, cx, cy = K
    R = c2w.copy()
    for _ in range(k):
        fx, fy, cx, cy = fy, fx, cy, W - 1 - cx
        R[:3, 0], R[:3, 1] = R[:3, 1].copy(), -R[:3, 0].copy()
        W, H = H, W
    return [fx, fy, cx, cy], R, W, H


def run(ctx: StageContext):
    import pycolmap

    out = ctx.out
    doc = ctx._doc
    stages = doc.get("stages", {})
    m = ctx.manifest()
    fr = ctx.fetch_stage("frames")
    dp = ctx.fetch_stage("depth")
    world = json.loads((dp / "world.json").read_text())
    rec = pycolmap.Reconstruction(str(dp / "sparse"))
    frames = {f["name"]: f for f in json.loads((fr / "frames.json").read_text())["frames"]}
    have = {s: ctx.has_stage(s) for s in ["lod", "splat", "densify", "plan"]}
    if not SPLAT_ENABLED:
        have["lod"] = have["splat"] = False
    bm = None
    if have["plan"]:
        pl = ctx.fetch_stage("plan")
        bm = json.loads((pl / "building_model.json").read_text())

    # ---------------- photos + cameras.json
    (out / "photos/full").mkdir(parents=True)
    (out / "photos/thumb").mkdir(parents=True)
    rooms = []
    if bm:
        from shapely.geometry import Point, Polygon

        rooms = [(r["id"], Polygon(r["polygon"])) for r in bm["storeys"][0]["rooms"]]
    t0 = min(f["t"] for f in frames.values())
    cams = []
    for im in sorted((i for i in rec.images.values() if i.has_pose), key=lambda i: i.name):
        name = im.name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        f = frames.get(name)
        img = cv2.imread(str(fr / "images" / im.name))
        if img is None or f is None:
            continue
        cam = rec.cameras[im.camera_id]
        c2w = np.linalg.inv(np.vstack([im.cam_from_world().matrix(), [0, 0, 0, 1]]))
        K = list(cam.params[:4])
        Wd, Hd = cam.width, cam.height
        k = upright_k(c2w)
        if k:
            img = np.ascontiguousarray(np.rot90(img, k))
            K, c2w, Wd, Hd = rotate_camera(K, c2w, Wd, Hd, k)
        s = min(1.0, 2048 / max(Wd, Hd))
        if s < 1:
            img = cv2.resize(img, (int(round(Wd * s)), int(round(Hd * s))), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
        sx, sy = w / Wd, h / Hd
        K = [K[0] * sx, K[1] * sy, (K[2] + 0.5) * sx - 0.5, (K[3] + 0.5) * sy - 0.5]
        cv2.imwrite(str(out / "photos/full" / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        ts = 256 / max(w, h)
        cv2.imwrite(str(out / "photos/thumb" / f"{name}.jpg"), cv2.resize(img, (int(w * ts), int(h * ts)), interpolation=cv2.INTER_AREA),
                    [cv2.IMWRITE_JPEG_QUALITY, 80])
        room_id = None
        if rooms:
            p = Point(c2w[0, 3], c2w[1, 3])
            room_id = next((rid for rid, P in rooms if P.contains(p)), None)
        cams.append({"id": name, "kind": f["kind"], "t": round(f["t"] - t0, 3), "full": f"photos/full/{name}.jpg",
                     "thumb": f"photos/thumb/{name}.jpg", "w": w, "h": h, "fx": round(K[0], 3), "fy": round(K[1], 3),
                     "cx": round(K[2], 3), "cy": round(K[3], 3), "c2w": [round(float(v), 6) for v in c2w.reshape(-1)],
                     "room_id": room_id})
    (out / "cameras.json").write_text(json.dumps({"cameras": cams}))
    if cams:
        mid = cams[len(cams) // 3]
        shutil.copyfile(out / mid["thumb"], out / "thumb.jpg")

    # ---------------- trajectory.json (ARKit poses -> World, ~10 Hz)
    W = np.asarray(world["world_from_sfm"])
    tt, pp = [], []
    for s in m["segments"]:
        lines = read_jsonl(ctx.fetch_raw(s["frames"]))
        last = -1.0
        for l in lines:
            if l["t"] - last < 0.1:
                continue
            last = l["t"]
            T = arkit_to_zup_cv(np.asarray(l["T"]).reshape(4, 4).T)
            c = W @ T[:, 3]
            tt.append(round(l["t"] - t0, 3))
            pp.append([round(float(v), 3) for v in c[:3]])
    (out / "trajectory.json").write_text(json.dumps({"t": tt, "p": pp}))

    # ---------------- copy artefacts
    def stats(stage):
        return (stages.get(stage) or {}).get("metrics") or {}

    layers = []
    lv = dict(world["levels"])
    if (lv.get("ceiling") or 0) < 2.0:  # unseen / mis-measured ceiling: match plan's 2.5 m fallback
        lv["ceiling"], lv["ceiling_max"] = 2.5, max(2.5, lv.get("ceiling_max") or 0)
    ver = f"v{ctx.version:02d}"
    if have["plan"]:
        (out / "model").mkdir()
        (out / "plan").mkdir()
        for p in pl.glob("*.glb"):
            shutil.copyfile(p, out / "model" / p.name)
        shutil.copyfile(pl / "building_model.json", out / "model/building_model.json")
        styles = []
        for p in sorted(pl.glob("plan_*.svg")):
            shutil.copyfile(p, out / "plan" / p.name)
            styles.append({"id": p.stem[5:], "url": f"plan/{p.name}"})
        st = bm["storeys"][0]
        if (out / "model/ceilings.glb").exists():
            cz = [r.get("ceiling_z") for r in st["rooms"] if r.get("ceiling_z")]
            layers.append({"id": "ceilings", "kind": "model", "label": "Ceilings", "url": "model/ceilings.glb", "visible": False,
                           "summary": f"{ver} · {min(cz):.2f}–{max(cz):.2f} m" if cz else ver})
    if have["lod"] or have["splat"]:
        (out / "splat").mkdir()
        sm = {**stats("splat"), **stats("lod")}
        if have["lod"]:
            lo = ctx.fetch_stage("lod")
            for n in ("scene.rad", "scene.spz", "scene.ply"):
                if (lo / n).exists():
                    shutil.copyfile(lo / n, out / "splat" / n)
            url = "splat/scene.rad"
            fb = "splat/scene.spz" if (out / "splat/scene.spz").exists() else "splat/scene.ply"
        else:
            shutil.copyfile(ctx.fetch_stage("splat", "scene.ply"), out / "splat/scene.ply")
            url, fb = "splat/scene.ply", None
        n = sm.get("splats_kept") or sm.get("gaussians") or sm.get("splats")
        layers.append({"id": "splat", "kind": "splat", "label": "Splat", "url": url, "fallback_url": fb, "visible": True,
                       "summary": f"iPhone · 3DGS · {round(n / 1000)} k" if n else "iPhone · 3DGS",
                       "stats": {"count": n, "sh_degree": 3, "lod_levels": sm.get("lod_levels"), "lod_nodes": sm.get("lod_nodes"),
                                 "psnr": sm.get("psnr")}})
    if have["plan"]:
        st = bm["storeys"][0]
        if (out / "model/architecture.glb").exists():
            layers.append({"id": "architecture", "kind": "model", "label": "Model", "url": "model/architecture.glb", "visible": True,
                           "summary": f"{ver} · walls · floors · doors · windows", "modes": ["solid", "edges"], "mode": "edges"})
        if (out / "model/furniture.glb").exists():
            layers.append({"id": "furniture", "kind": "model", "label": "Model", "url": "model/furniture.glb", "visible": True,
                           "summary": f"{ver} · furniture · {len(st['furniture'])}", "modes": ["solid", "edges"], "mode": "edges"})
    (out / "points").mkdir()
    shutil.copyfile(dp / "points.ply", out / "points/points.ply")
    npts = stats("depth").get("points")
    layers.append({"id": "pointcloud", "kind": "points", "label": "Point cloud", "url": "points/points.ply", "visible": False,
                   "summary": f"LiDAR · {npts / 1e6:.1f} M" if npts else "LiDAR fused"})
    if have["densify"]:
        shutil.copyfile(ctx.fetch_stage("densify", "dense.ply"), out / "points/dense.ply")
        dm = stats("densify")
        dn = dm.get("points")
        layers.append({"id": "dense", "kind": "points", "label": "Dense cloud", "url": "points/dense.ply", "visible": False,
                       "summary": f"DA3 · {dn / 1e6:.1f} M" if dn else "DA3 dense"})
    layers.append({"id": "photos", "kind": "cameras", "label": "Photos", "url": "cameras.json", "visible": True,
                   "summary": f"{len(cams)} photos", "modes": ["keyframes", "hero", "all"], "mode": "keyframes"})
    layers.append({"id": "trajectory", "kind": "trajectory", "label": "Trajectory", "url": "trajectory.json", "visible": False,
                   "summary": f"{stats('ingest').get('path_m', '')} m walked"})
    if have["plan"] and styles:
        layers.append({"id": "plan", "kind": "plan", "label": "Plan", "visible": True, "summary": "Stylized plan", "styles": styles,
                       "style": styles[0]["id"] if not any(s["id"] == "stylized" for s in styles) else "stylized"})
    layers.append({"id": "ground", "kind": "ground", "label": "Ground", "visible": True, "cell": 0.5, "fade": 4.0,
                   "modes": ["lines", "checker"], "mode": "lines"})

    b = world["bounds"]
    ctr = [(b["min"][0] + b["max"][0]) / 2, (b["min"][1] + b["max"][1]) / 2]
    ext = max(b["max"][0] - b["min"][0], b["max"][1] - b["min"][1])
    proc = sum(float((stages.get(s) or {}).get("duration_s") or 0) for s in stages)
    scene = {"schema": "scene.v1", "scan_id": ctx.scan_id, "version": ctx.version, "name": doc.get("name") or m.get("name"),
             "captured_at": m.get("created_at"), "created_at": now_iso(), "units": "m", "up": "z",
             "north_deg": world.get("north_deg"), "bounds": b,
             "levels": {"floor": 0.0, "ceiling": lv.get("ceiling"), "ceiling_max": lv.get("ceiling_max")},
             "default_view": {"position": [ctr[0] + 0.35 * ext, ctr[1] - 0.5 * ext, 1.15 * ext], "target": [ctr[0], ctr[1], 0.5]},
             "layers": layers, "building_model_url": "model/building_model.json" if have["plan"] else None,
             "stats": {"processing_s": round(proc), "frames": stats("frames").get("keyframes"),
                       "registered": stats("sfm").get("registered"), "sfm": stats("sfm").get("mapper")}}
    (out / "scene.json").write_text(json.dumps(scene, indent=1))
    # upload published tree (separate from the work/ copy the runner makes)
    ctx.store.upload_dir(out, ctx.processed_prefix)
    viewer_path = f"/scans/{ctx.scan_id}?v={ctx.version}"
    versions = sorted(set((doc.get("versions") or []) + [ctx.version]))
    failed = [s for s, v in stages.items() if (v or {}).get("status") == "failed" and (v or {}).get("version") == ctx.version]
    ctx.status.update({"status": "ready", "stage": "publish", "current_version": ctx.version, "versions": versions,
                       "viewer_path": viewer_path, "thumb": f"{ctx.processed_prefix}/thumb.jpg", "ready_at": now_iso(),
                       "partial": bool(failed), "failed_stages": failed, "layers": [l["id"] for l in layers]})
    ctx.metrics.update({"layers": [l["id"] for l in layers], "photos": len(cams), "partial": bool(failed)})
    notify(ctx, doc, failed)


def notify(ctx: StageContext, doc: dict, failed: list[str]):
    """Best-effort Expo push to the owner's devices."""
    try:
        import requests
        from google.cloud import firestore

        owner = doc.get("owner")
        db = firestore.Client()
        toks = [d.to_dict().get("token") for d in db.collection("devices").where("owner", "==", owner).stream()] if owner else []
        toks = [t for t in toks if t]
        if not toks:
            return
        body = [{"to": t, "title": f"{doc.get('name') or 'Scan'} is ready" + (" (partial)" if failed else ""),
                 "body": "Tap to open the 3D viewer.", "data": {"scan_id": ctx.scan_id, "version": ctx.version}} for t in toks]
        requests.post("https://exp.host/--/api/v2/push/send", json=body, timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("push notification failed: %s", e)


_ = Path
