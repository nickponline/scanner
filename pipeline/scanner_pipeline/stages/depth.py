"""Stage 4 — LiDAR depth preparation, TSDF fusion and World frame (REQUIREMENTS §5.4).

1. Filter LiDAR depth (ARKit confidence, range, flying pixels).
2. TSDF-fuse with SfM poses -> dense coloured point cloud + fallback mesh.
3. Detect floor / ceiling heights and the Manhattan (dominant wall) direction.
4. Define World: Z up, floor z=0, walls axis-aligned, XY origin at the floor bbox centre.
5. Re-check every LiDAR map against the fused surface (multi-view consensus) -> clean depth for splatting.

Outputs: sparse/ (COLMAP model in World), depth/{name}.npz (depth f16, valid mask), points.ply, tsdf_mesh.ply,
         world.json (levels, transform, north)
"""
from __future__ import annotations

import json
import math

import cv2
import numpy as np

from ..ctx import StageContext
from ..geom import bearing_of, rot_z

DEFAULTS = {"min_conf": 2, "min_depth": 0.15, "max_depth": 6.0, "voxel": 0.02, "trunc": 0.08,
            "consensus_rel": 0.05, "points_voxel": 0.01, "max_points": 6_000_000,
            "ceiling_min_m": 2.0, "ceiling_min_pts": 500, "ceiling_min_frac": 0.002}


def flying_pixel_mask(d: np.ndarray, rel: float = 0.06) -> np.ndarray:
    gx = np.abs(cv2.Sobel(d, cv2.CV_32F, 1, 0, ksize=3)) / 8
    gy = np.abs(cv2.Sobel(d, cv2.CV_32F, 0, 1, ksize=3)) / 8
    return (np.maximum(gx, gy) / np.maximum(d, 1e-3)) < rel


def mode_z(z: np.ndarray, bin_size=0.02):
    if len(z) == 0:
        return None
    lo, hi = np.percentile(z, 0.5), np.percentile(z, 99.5)
    bins = np.arange(lo, hi + bin_size, bin_size)
    if len(bins) < 2:
        return float(np.median(z))
    h, e = np.histogram(z, bins)
    if len(h) >= 3:  # np.convolve "same" returns max(len) samples, so only smooth when h is longer than the kernel
        h = np.convolve(h, [1, 2, 1], "same")
    i = int(np.argmax(h))
    return float((e[i] + e[i + 1]) / 2)


def manhattan_angle(normals: np.ndarray) -> float:
    """Dominant wall direction in [0, 90) degrees from horizontal normals."""
    a = np.degrees(np.arctan2(normals[:, 1], normals[:, 0])) % 90.0
    h, _ = np.histogram(a, bins=90, range=(0, 90))
    k = np.exp(-0.5 * (np.arange(-4, 5) / 1.5) ** 2)
    hs = np.convolve(np.concatenate([h[-4:], h, h[:4]]), k, "same")[4:-4]
    i = int(np.argmax(hs))
    # parabolic refinement
    l, c, r = hs[(i - 1) % 90], hs[i], hs[(i + 1) % 90]
    off = 0.5 * (l - r) / max(l - 2 * c + r, -1e-9) if (l - 2 * c + r) < 0 else 0.0
    ang = (i + 0.5 + off) % 90.0
    return ang if ang < 45 else ang - 90  # smallest rotation


def _sharpness(P: np.ndarray, t: float, res: float) -> float:
    c, s_ = math.cos(math.radians(-t)), math.sin(math.radians(-t))
    x = c * P[:, 0] - s_ * P[:, 1]
    y = s_ * P[:, 0] + c * P[:, 1]
    hx = np.bincount(((x - x.min()) / res).astype(int)).astype(np.float64)
    hy = np.bincount(((y - y.min()) / res).astype(int)).astype(np.float64)
    return float((hx ** 2).sum() + (hy ** 2).sum())


def refine_manhattan(P: np.ndarray, theta0: float, res=0.02) -> float:
    """Rotation (deg, in [-45, 45)) that makes the x/y histograms of wall points sharpest.
    Full 90-degree coarse sweep (the normal-histogram seed theta0 can be >10 deg off), then fine search."""
    if len(P) < 200:
        return theta0
    if len(P) > 300_000:
        P = P[np.random.default_rng(0).choice(len(P), 300_000, replace=False)]
    coarse = np.arange(-45.0, 45.0, 0.5)
    sc = [_sharpness(P, t, res) for t in coarse]
    t0 = float(coarse[int(np.argmax(sc))])
    fine = np.arange(t0 - 0.6, t0 + 0.6 + 1e-9, 0.05)
    t = float(fine[int(np.argmax([_sharpness(P, t, res) for t in fine]))])
    return float((t + 45) % 90 - 45)


def run(ctx: StageContext):
    import open3d as o3d
    import pycolmap

    cfg = {**DEFAULTS, **ctx.config.get("depth", {})}
    fr = ctx.fetch_stage("frames")
    sfm = ctx.fetch_stage("sfm")
    frames = {f["name"]: f for f in json.loads((fr / "frames.json").read_text())["frames"]}
    rec = pycolmap.Reconstruction(str(sfm / "sparse"))
    m = ctx.manifest()

    dev = o3d.core.Device("CUDA:0") if o3d.core.cuda.is_available() else o3d.core.Device("CPU:0")
    vbg = o3d.t.geometry.VoxelBlockGrid(
        attr_names=("tsdf", "weight", "color"), attr_dtypes=(o3d.core.float32,) * 3,
        attr_channels=((1), (1), (3)), voxel_size=cfg["voxel"], block_resolution=8, block_count=400_000, device=dev)
    used = []
    imgs = sorted((i for i in rec.images.values() if i.has_pose), key=lambda im: im.name)
    for n, im in enumerate(imgs):
        name = im.name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        f = frames.get(name)
        if not f or not f.get("depth"):
            continue
        z = np.load(fr / f["depth"])
        d = z["depth"].astype(np.float32)
        conf = z["conf"]
        valid = (conf >= cfg["min_conf"]) & (d > cfg["min_depth"]) & (d < cfg["max_depth"]) & flying_pixel_mask(d)
        d = np.ascontiguousarray(np.where(valid, d, 0).astype(np.float32))
        if valid.mean() < 0.05:
            continue
        h, w = d.shape
        cam = rec.cameras[im.camera_id]
        fx, fy, cx, cy = cam.params[:4]
        s = w / cam.width
        Kd = np.array([[fx * s, 0, (cx + 0.5) * s - 0.5], [0, fy * s, (cy + 0.5) * s - 0.5], [0, 0, 1]])
        color = cv2.cvtColor(cv2.imread(str(fr / "images" / im.name)), cv2.COLOR_BGR2RGB)
        color = np.ascontiguousarray(cv2.resize(color, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32) / 255)
        w2c = np.vstack([im.cam_from_world().matrix(), [0, 0, 0, 1]])
        K_t = o3d.core.Tensor(Kd, o3d.core.float64)
        E_t = o3d.core.Tensor(w2c, o3d.core.float64)
        dt = o3d.t.geometry.Image(o3d.core.Tensor(d[..., None])).to(dev)
        ct = o3d.t.geometry.Image(o3d.core.Tensor(color)).to(dev)
        blk = vbg.compute_unique_block_coordinates(dt, K_t, E_t, 1.0, cfg["max_depth"])
        vbg.integrate(blk, dt, ct, K_t, K_t, E_t, 1.0, cfg["max_depth"])
        used.append((im, name, d, Kd))
        if n % 25 == 0:
            ctx.progress(0.6 * n / len(imgs), f"tsdf {n}/{len(imgs)}")
    if not used:
        raise RuntimeError("no registered images have LiDAR depth")

    mesh = vbg.extract_triangle_mesh(weight_threshold=3.0).to_legacy()
    mesh.compute_vertex_normals()
    pcd = o3d.geometry.PointCloud(mesh.vertices)
    pcd.colors = mesh.vertex_colors
    pcd.normals = mesh.vertex_normals
    P = np.asarray(pcd.points)
    N = np.asarray(pcd.normals)
    ctx.progress(0.65, "levels + manhattan")

    # ---- World frame
    vert = (np.abs(N[:, 2]) < 0.2)
    theta0 = manhattan_angle(N[vert])
    zz = P[:, 2] - np.percentile(P[:, 2], 2)
    wallpts = P[vert & (zz > 0.3) & (zz < 2.0)]
    theta = refine_manhattan(wallpts[:, :2], theta0)
    ctx.metrics["manhattan_initial_deg"] = round(float(theta0), 2)
    R = rot_z(-math.radians(theta))
    Pr = P @ R[:3, :3].T
    up = N[:, 2] > 0.9
    zfloor_cand = Pr[up, 2]
    floor = mode_z(zfloor_cand[zfloor_cand < np.percentile(Pr[:, 2], 40)]) if up.sum() > 100 else float(np.percentile(Pr[:, 2], 1))
    down = N[:, 2] < -0.9
    # ceiling: downward-facing surfaces >= ceiling_min_m above the floor, with real support. Captures that never look
    # up otherwise "find" a ceiling on shelf / cabinet undersides (1.76 m on one house) and the CAD walls get that height.
    cz = Pr[down, 2][Pr[down, 2] > floor + cfg["ceiling_min_m"]]
    enough = len(cz) >= max(cfg["ceiling_min_pts"], cfg["ceiling_min_frac"] * len(Pr))
    ceiling = mode_z(cz) if enough else None
    ceiling_max = float(np.percentile(cz, 98)) if enough else None
    ctx.metrics["ceiling_support_pts"] = int(len(cz))
    fl = up & (np.abs(Pr[:, 2] - floor) < 0.05)
    fxy = Pr[fl, :2] if fl.sum() > 100 else Pr[:, :2]
    lo, hi = np.percentile(fxy, 1, axis=0), np.percentile(fxy, 99, axis=0)
    ctr = (lo + hi) / 2
    T = np.eye(4)
    T[:3, 3] = [-ctr[0], -ctr[1], -floor]
    W = T @ R  # zup(SfM) -> World

    # apply to model
    q = pycolmap.Rotation3d(W[:3, :3])
    rec.transform(pycolmap.Sim3d(1.0, q, W[:3, 3]))
    (ctx.out / "sparse").mkdir()
    rec.write(str(ctx.out / "sparse"))
    pcd.transform(W)
    mesh.transform(W)

    # ---- consensus filter of LiDAR maps against fused surface
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
    (ctx.out / "depth").mkdir()
    kept = []
    for n, (im, name, d, Kd) in enumerate(used):
        im_w = rec.images[im.image_id]
        h, w = d.shape
        c2w = np.linalg.inv(np.vstack([im_w.cam_from_world().matrix(), [0, 0, 0, 1]]))
        rays = o3d.t.geometry.RaycastingScene.create_rays_pinhole(
            o3d.core.Tensor(Kd), o3d.core.Tensor(np.linalg.inv(c2w)), w, h)
        t_hit = scene.cast_rays(rays)["t_hit"].numpy()
        zr = t_hit  # create_rays_pinhole directions have z=1, so t_hit is z-depth
        ok = (d > 0) & np.isfinite(zr) & (np.abs(d - zr) < cfg["consensus_rel"] * d + 0.01)
        kept.append(ok.sum() / max(1, (d > 0).sum()))
        np.savez_compressed(ctx.out / "depth" / f"{name}.npz", depth=np.where(ok, d, 0).astype(np.float16))
        if n % 25 == 0:
            ctx.progress(0.7 + 0.25 * n / len(used), f"consensus {n}/{len(used)}")

    # ---- points.ply (contract: xyz f32, rgb u8, normals f32)
    pcd = pcd.voxel_down_sample(cfg["points_voxel"])
    if len(pcd.points) > cfg["max_points"]:
        pcd = pcd.random_down_sample(cfg["max_points"] / len(pcd.points))
    write_points_ply(ctx.out / "points.ply", np.asarray(pcd.points), np.asarray(pcd.colors), np.asarray(pcd.normals))
    o3d.io.write_triangle_mesh(str(ctx.out / "tsdf_mesh.ply"), mesh)

    heading = m.get("heading_deg_at_start")
    north = None
    if heading is not None:
        # World +Y expressed in the zup frame; zup +Y bearing == ARKit -Z bearing == heading
        north = bearing_of((W[:3, :3].T @ np.array([0.0, 1.0, 0.0]))[:2], heading)
    if ceiling is not None:
        ceiling -= floor
    if ceiling_max is not None:
        ceiling_max -= floor
    pts = np.asarray(pcd.points)
    world = {"levels": {"floor": 0.0, "ceiling": round(ceiling, 3) if ceiling else None,
                        "ceiling_max": round(ceiling_max, 3) if ceiling_max else None},
             "manhattan_deg": round(theta, 3), "world_from_sfm": W.tolist(), "north_deg": north,
             "bounds": {"min": pts.min(0).round(3).tolist(), "max": pts.max(0).round(3).tolist()}}
    (ctx.out / "world.json").write_text(json.dumps(world, indent=1))
    ctx.metrics.update({"depth_images": len(used), "points": len(pcd.points), "mesh_tris": len(mesh.triangles),
                        "consensus_keep_mean": round(float(np.mean(kept)), 3), "ceiling": world["levels"]["ceiling"],
                        "ceiling_source": "measured" if world["levels"]["ceiling"] else "default (not seen)",
                        "manhattan_deg": world["manhattan_deg"]})


def write_points_ply(path, xyz, rgb01, nrm):
    n = len(xyz)
    dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1"),
                   ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")])
    a = np.empty(n, dt)
    a["x"], a["y"], a["z"] = xyz.T.astype(np.float32)
    c = np.clip(rgb01 * 255 + 0.5, 0, 255).astype(np.uint8) if len(rgb01) else np.zeros((n, 3), np.uint8)
    a["red"], a["green"], a["blue"] = c.T
    nn = nrm if len(nrm) else np.zeros((n, 3))
    a["nx"], a["ny"], a["nz"] = nn.T.astype(np.float32)
    hdr = ("ply\nformat binary_little_endian 1.0\n"
           f"element vertex {n}\n"
           "property float x\nproperty float y\nproperty float z\n"
           "property uchar red\nproperty uchar green\nproperty uchar blue\n"
           "property float nx\nproperty float ny\nproperty float nz\nend_header\n")
    with open(path, "wb") as f:
        f.write(hdr.encode())
        f.write(a.tobytes())
