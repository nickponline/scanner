"""Stage 5 — dense point cloud from Depth Anything 3 with known poses (replaces the CoMe mesh stage).

1. Run DA3 (pose-conditioned: our World-frame SfM extrinsics + intrinsics go in, so depth comes out in the
   metric scale of our poses) over windows of time-consecutive views plus a few spatial-neighbour context views.
2. Per view: align the DA3 depth to the consensus-filtered LiDAR depth (robust median scale) and record the
   residual, so a bad window is visible in the report and can't silently bend the cloud.
3. Filter: DA3 confidence (per-view percentile), flying pixels at depth edges, depth range, and multi-view
   geometric consistency (a pixel survives if >= min_consistent neighbour depth maps agree within rel_tol).
4. Fuse: voxel-average xyz/rgb/normal, crop to the scan bounds, cap the point count by growing the voxel.

Outputs: dense.ply (xyz f32, rgb u8, normals f32 — same contract as depth/points.ply), views.json (per-view
stats), preview/*.jpg (RGB | DA3 depth | kept mask) for the report.
"""
from __future__ import annotations

import json
import math
import time

import cv2
import numpy as np

from ..ctx import StageContext
from .depth import flying_pixel_mask, write_points_ply

DEFAULTS = {"model": "depth-anything/DA3NESTED-GIANT-LARGE-1.1", "process_res": 504, "chunk": 16, "context": 8,
            "pose_conditioned": True, "lidar_align": True, "lidar_scale_clip": [0.8, 1.25],
            "conf_drop_pct": 25.0, "min_depth": 0.1, "max_depth": 8.0, "edge_rel": 0.05,
            "neighbors": 6, "min_consistent": 2, "rel_tol": 0.03, "max_angle_deg": 70.0,
            "voxel": 0.008, "max_points": 3_000_000, "bounds_margin": 0.4, "previews": 6}


# ---------------------------------------------------------------- geometry helpers (numpy, unit-tested)

def cam_center_dir(w2c: np.ndarray):
    R, t = w2c[:3, :3], w2c[:3, 3]
    return -R.T @ t, R[2]  # optical axis in world = third row of R


def neighbours(centers: np.ndarray, dirs: np.ndarray, k: int, max_angle_deg: float) -> list[list[int]]:
    """k nearest cameras (by centre distance, with a penalty for view-direction difference) looking within
    max_angle of each other."""
    n = len(centers)
    d = np.linalg.norm(centers[:, None] - centers[None], axis=-1)
    cosang = np.clip(dirs @ dirs.T, -1, 1)
    ang = np.degrees(np.arccos(cosang))
    score = d + 0.01 * ang  # 0.5 m ~ 50 deg
    score[ang > max_angle_deg] = np.inf
    np.fill_diagonal(score, np.inf)
    out = []
    for i in range(n):
        o = np.argsort(score[i])[:k]
        out.append([int(j) for j in o if np.isfinite(score[i, j])])
    return out


def windows(n: int, chunk: int) -> list[list[int]]:
    return [list(range(s, min(n, s + chunk))) for s in range(0, n, chunk)]


def with_context(core: list[int], nbrs: list[list[int]], context: int) -> list[int]:
    """Core views + the most-shared spatial neighbours outside the core (loop-closure context)."""
    cs = set(core)
    votes: dict[int, int] = {}
    for i in core:
        for r, j in enumerate(nbrs[i]):
            if j not in cs:
                votes[j] = votes.get(j, 0) + (len(nbrs[i]) - r)
    extra = sorted(votes, key=lambda j: -votes[j])[:context]
    return core + extra


def unproject(depth: np.ndarray, K: np.ndarray, c2w: np.ndarray) -> np.ndarray:
    h, w = depth.shape
    u, v = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    x = (u - K[0, 2]) / K[0, 0] * depth
    y = (v - K[1, 2]) / K[1, 1] * depth
    P = np.stack([x, y, depth], -1).reshape(-1, 3)
    return (P @ c2w[:3, :3].T + c2w[:3, 3]).astype(np.float32)


def normals_from_points(P: np.ndarray, h: int, w: int, c2w: np.ndarray) -> np.ndarray:
    G = P.reshape(h, w, 3)
    dx = np.zeros_like(G)
    dy = np.zeros_like(G)
    dx[:, 1:-1] = G[:, 2:] - G[:, :-2]
    dy[1:-1] = G[2:] - G[:-2]
    n = np.cross(dx, dy).reshape(-1, 3)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
    to_cam = c2w[:3, 3][None] - P
    flip = (n * to_cam).sum(1) < 0
    n[flip] *= -1
    return n.astype(np.float32)


def consistency_count(P: np.ndarray, others: list[tuple[np.ndarray, np.ndarray, np.ndarray]], rel_tol: float) -> np.ndarray:
    """For world points P, count neighbour depth maps (depth, K, w2c) that see the same surface within rel_tol."""
    cnt = np.zeros(len(P), np.int16)
    for D, K, w2c in others:
        h, w = D.shape
        X = P @ w2c[:3, :3].T + w2c[:3, 3]
        z = X[:, 2]
        ok = z > 1e-3
        u = np.full(len(P), -1, np.int64)
        v = np.full(len(P), -1, np.int64)
        u[ok] = np.round(K[0, 0] * X[ok, 0] / z[ok] + K[0, 2]).astype(np.int64)
        v[ok] = np.round(K[1, 1] * X[ok, 1] / z[ok] + K[1, 2]).astype(np.int64)
        ok &= (u >= 0) & (u < w) & (v >= 0) & (v < h)
        idx = np.flatnonzero(ok)
        dj = D[v[idx], u[idx]]
        good = (dj > 0) & (np.abs(dj - z[idx]) < rel_tol * z[idx])
        cnt[idx[good]] += 1
    return cnt


def voxel_fuse(P: np.ndarray, C: np.ndarray, N: np.ndarray, voxel: float):
    """Average points, colours and normals per voxel. Returns (P, C, N, counts)."""
    q = np.floor(P / voxel).astype(np.int64)
    q -= q.min(0)
    dims = q.max(0) + 1
    key = (q[:, 0] * dims[1] + q[:, 1]) * dims[2] + q[:, 2]
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    m = len(cnt)

    def avg(A):
        return np.stack([np.bincount(inv, A[:, c], minlength=m) for c in range(A.shape[1])], 1) / cnt[:, None]

    Pn, Cn, Nn = avg(P.astype(np.float64)), avg(C.astype(np.float64)), avg(N.astype(np.float64))
    Nn /= np.maximum(np.linalg.norm(Nn, axis=1, keepdims=True), 1e-9)
    return Pn.astype(np.float32), Cn.astype(np.float32), Nn.astype(np.float32), cnt


def lidar_scale(da3: np.ndarray, lidar: np.ndarray | None):
    """Robust scale s with lidar ~= s * da3 (both z-depth), and abs-rel error before / after."""
    if lidar is None:
        return None
    h, w = lidar.shape
    d = cv2.resize(da3, (w, h), interpolation=cv2.INTER_AREA)
    ok = (lidar > 0) & (d > 0)
    if ok.sum() < 200:
        return None
    r = lidar[ok] / d[ok]
    s = float(np.median(r))
    before = float(np.median(np.abs(d[ok] - lidar[ok]) / lidar[ok]))
    after = float(np.median(np.abs(s * d[ok] - lidar[ok]) / lidar[ok]))
    return {"scale": s, "abs_rel_before": before, "abs_rel_after": after, "n": int(ok.sum())}


def colorize_depth(d: np.ndarray, lo: float, hi: float) -> np.ndarray:
    x = np.clip((d - lo) / max(hi - lo, 1e-6), 0, 1)
    img = cv2.applyColorMap((x * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    img[d <= 0] = 0
    return img


# ---------------------------------------------------------------- stage

def run(ctx: StageContext):
    import pycolmap
    import torch
    from depth_anything_3.api import DepthAnything3

    cfg = {**DEFAULTS, **ctx.config.get("densify", {})}
    fr = ctx.fetch_stage("frames", "images")
    dp = ctx.fetch_stage("depth")
    rec = pycolmap.Reconstruction(str(dp / "sparse"))
    world = json.loads((dp / "world.json").read_text())

    views = []
    for im in sorted((i for i in rec.images.values() if i.has_pose), key=lambda i: i.name):
        path = fr / "images" / im.name
        if not path.exists():
            continue
        cam = rec.cameras[im.camera_id]
        fx, fy, cx, cy = cam.params[:4]
        w2c = np.eye(4)
        w2c[:3] = im.cam_from_world().matrix()
        name = im.name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        lp = dp / "depth" / f"{name}.npz"
        views.append({"name": name, "path": path, "W": cam.width, "H": cam.height, "w2c": w2c,
                      "K": np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], np.float64),
                      "lidar": lp if lp.exists() else None})
    if not views:
        raise RuntimeError("no posed views")
    n = len(views)
    cd = [cam_center_dir(v["w2c"]) for v in views]
    centers, dirs = np.array([c for c, _ in cd]), np.array([d for _, d in cd])
    nbrs = neighbours(centers, dirs, max(cfg["neighbors"], cfg["context"]), cfg["max_angle_deg"])

    # windows only mix views of the same size (DA3 centre-crops mismatched batches)
    groups: dict[tuple[int, int], list[int]] = {}
    for i, v in enumerate(views):
        groups.setdefault((v["W"], v["H"]), []).append(i)
    jobs = []
    for idx in groups.values():
        for core in windows(len(idx), cfg["chunk"]):
            core_i = [idx[k] for k in core]
            ctx_i = [j for j in with_context(core_i, nbrs, cfg["context"]) if (views[j]["W"], views[j]["H"]) == (views[core_i[0]]["W"], views[core_i[0]]["H"])]
            jobs.append((core_i, ctx_i))

    ctx.progress(0.02, f"loading {cfg['model']}")
    t0 = time.time()
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = DepthAnything3.from_pretrained(cfg["model"]).to(dev).eval()
    load_s = time.time() - t0

    # ---- 1. DA3 depth per view (core outputs only)
    depth: dict[int, np.ndarray] = {}
    conf: dict[int, np.ndarray] = {}
    Kd: dict[int, np.ndarray] = {}
    rgb: dict[int, np.ndarray] = {}
    pose_mode = "pose_conditioned" if cfg["pose_conditioned"] else "unposed"
    pose_fail = None
    t1 = time.time()
    for jn, (core, allv) in enumerate(jobs):
        imgs = [cv2.cvtColor(cv2.imread(str(views[i]["path"])), cv2.COLOR_BGR2RGB) for i in allv]
        # K at the input resolution (DA3 rescales it with the image)
        Ks = np.stack([views[i]["K"] for i in allv]).astype(np.float32)
        Es = np.stack([views[i]["w2c"] for i in allv]).astype(np.float32)
        kw = dict(process_res=cfg["process_res"])
        pred = None
        if pose_mode == "pose_conditioned":
            try:
                pred = model.inference(imgs, extrinsics=Es, intrinsics=Ks, align_to_input_ext_scale=True, **kw)
            except Exception as e:  # noqa: BLE001
                pose_fail = f"{type(e).__name__}: {e}"[:400]
                pose_mode = "unposed"
        if pred is None:
            pred = model.inference(imgs, **kw)
        for k, i in enumerate(allv):
            if i not in core:
                continue
            d = np.asarray(pred.depth[k], np.float32)
            h, w = d.shape
            V = views[i]
            sx, sy = w / V["W"], h / V["H"]
            fx, fy, cx, cy = V["K"][0, 0], V["K"][1, 1], V["K"][0, 2], V["K"][1, 2]
            Kd[i] = np.array([[fx * sx, 0, (cx + 0.5) * sx - 0.5], [0, fy * sy, (cy + 0.5) * sy - 0.5], [0, 0, 1]])
            depth[i] = d
            conf[i] = np.asarray(pred.conf[k], np.float32)
            rgb[i] = np.asarray(pred.processed_images[k], np.uint8)
        ctx.progress(0.05 + 0.5 * (jn + 1) / len(jobs), f"DA3 window {jn + 1}/{len(jobs)} ({pose_mode})")
    infer_s = time.time() - t1
    del model
    if dev.type == "cuda":
        torch.cuda.empty_cache()

    # ---- 2. LiDAR scale check + 3. per-view filtering
    per_view = []
    lo_s, hi_s = cfg["lidar_scale_clip"]
    masks: dict[int, np.ndarray] = {}
    for i in sorted(depth):
        V = views[i]
        d = depth[i]
        lid = np.load(V["lidar"])["depth"].astype(np.float32) if V["lidar"] is not None else None
        st = lidar_scale(d, lid)
        applied = 1.0
        if cfg["lidar_align"] and st is not None and lo_s <= st["scale"] <= hi_s:
            applied = st["scale"]
        elif pose_mode == "unposed" and st is not None:
            applied = st["scale"]  # unposed depth has no metric scale of its own
        d = d * applied
        thr = np.percentile(conf[i], cfg["conf_drop_pct"])
        m = (conf[i] >= thr) & (d > cfg["min_depth"]) & (d < cfg["max_depth"]) & flying_pixel_mask(d, cfg["edge_rel"])
        depth[i] = np.where(m, d, 0).astype(np.float32)
        masks[i] = m
        per_view.append({"name": V["name"], "scale": round(st["scale"], 4) if st else None, "applied": round(applied, 4),
                         "abs_rel_before": round(st["abs_rel_before"], 4) if st else None,
                         "abs_rel_after": round(st["abs_rel_after"], 4) if st else None,
                         "lidar_px": st["n"] if st else 0, "conf_edge_kept": round(float(m.mean()), 4)})
    ctx.progress(0.6, "multi-view consistency")

    # ---- 3b. geometric consistency + accumulate
    b = world["bounds"]
    mg = cfg["bounds_margin"]
    blo, bhi = np.array(b["min"]) - mg, np.array(b["max"]) + mg
    Ps, Cs, Ns = [], [], []
    pv = {p["name"]: p for p in per_view}
    order = sorted(depth)
    prev_idx = set(np.linspace(0, len(order) - 1, min(cfg["previews"], len(order))).round().astype(int).tolist())
    (ctx.out / "preview").mkdir()
    for c_, i in enumerate(order):
        V = views[i]
        d = depth[i]
        h, w = d.shape
        c2w = np.linalg.inv(V["w2c"])
        P = unproject(d, Kd[i], c2w)
        sel = d.reshape(-1) > 0
        others = [(depth[j], Kd[j], views[j]["w2c"]) for j in nbrs[i][:cfg["neighbors"]] if j in depth]
        cnt = np.zeros(len(P), np.int16)
        cnt[sel] = consistency_count(P[sel], others, cfg["rel_tol"])
        keep = sel & (cnt >= min(cfg["min_consistent"], max(1, len(others)))) & np.all((P >= blo) & (P <= bhi), axis=1)
        Nrm = normals_from_points(P, h, w, c2w)
        Ps.append(P[keep])
        Cs.append(rgb[i].reshape(-1, 3)[keep].astype(np.float32) / 255.0)
        Ns.append(Nrm[keep])
        pv[V["name"]]["kept"] = round(float(keep.mean()), 4)
        if c_ in prev_idx:
            dd = depth[i]
            pos = dd[dd > 0]
            lo, hi = (np.percentile(pos, 2), np.percentile(pos, 98)) if len(pos) else (0, 1)
            # BGR: kept = teal accent, dropped = dark grey
            km = np.where(keep.reshape(h, w)[..., None], np.array([176, 196, 46]), np.array([52, 50, 46])).astype(np.uint8)
            panel = np.concatenate([cv2.cvtColor(rgb[i], cv2.COLOR_RGB2BGR), colorize_depth(dd, lo, hi), km], 1)
            cv2.imwrite(str(ctx.out / "preview" / f"{V['name']}.jpg"), panel, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if c_ % 20 == 0:
            ctx.progress(0.6 + 0.3 * c_ / len(order), f"consistency {c_}/{len(order)}")
    P = np.concatenate(Ps)
    C = np.concatenate(Cs)
    N = np.concatenate(Ns)
    raw_pts = len(P)
    if raw_pts == 0:
        raise RuntimeError("no points survived filtering")

    # ---- 4. fuse
    ctx.progress(0.92, f"voxel fusion of {raw_pts / 1e6:.1f} M points")
    voxel = cfg["voxel"]
    while True:
        Pf, Cf, Nf, cnt = voxel_fuse(P, C, N, voxel)
        if len(Pf) <= cfg["max_points"]:
            break
        voxel *= math.sqrt(len(Pf) / cfg["max_points"]) * 1.02
    write_points_ply(ctx.out / "dense.ply", Pf, Cf, Nf)
    (ctx.out / "views.json").write_text(json.dumps({"views": per_view}, indent=0))

    sc = [p["applied"] for p in per_view]
    ab = [p["abs_rel_after"] for p in per_view if p["abs_rel_after"] is not None]
    ab0 = [p["abs_rel_before"] for p in per_view if p["abs_rel_before"] is not None]
    raw_scales = [p["scale"] for p in per_view if p["scale"] is not None]
    ctx.metrics.update({
        "model": cfg["model"], "mode": pose_mode, "pose_fail": pose_fail, "views": len(depth), "windows": len(jobs),
        "process_res": cfg["process_res"], "depth_res": list(next(iter(depth.values())).shape[::-1]),
        "points": int(len(Pf)), "points_raw": int(raw_pts), "voxel_m": round(voxel, 4),
        "obs_per_point_p50": float(np.median(cnt)),
        "kept_frac": round(raw_pts / sum(d.size for d in depth.values()), 4),
        "lidar_scale_p50": round(float(np.median(raw_scales)), 4) if raw_scales else None,
        "lidar_scale_p10": round(float(np.percentile(raw_scales, 10)), 4) if raw_scales else None,
        "lidar_scale_p90": round(float(np.percentile(raw_scales, 90)), 4) if raw_scales else None,
        "lidar_aligned_views": int(sum(1 for s in sc if s != 1.0)),
        "abs_rel_vs_lidar_before": round(float(np.median(ab0)), 4) if ab0 else None,
        "abs_rel_vs_lidar_after": round(float(np.median(ab)), 4) if ab else None,
        "model_load_s": round(load_s, 1), "infer_s": round(infer_s, 1),
        "ply_mb": round((ctx.out / "dense.ply").stat().st_size / 1e6, 1)})
