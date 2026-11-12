"""Stage 7 — mesh extraction with CoMe (REQUIREMENTS §5.7), TSDF fallback.

CoMe (Radl et al., ECCV 2026) is run with its ScanNet++ (indoor) settings on a COLMAP-format
dataset made from the World-frame SfM model. Post-processing: crop to the scan bounds, drop small
components, decimate, colour vertices from the fused point cloud, then export GLB
(gltfpack meshopt compression) + PLY.

The LiDAR-depth-supervised CoMe variant (R5.7.2) is not implemented yet.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np

from ..ctx import StageContext, sh

COME_DIR = Path(os.environ.get("COME_DIR", "/opt/CoMe"))
DEFAULTS = {"engine": "come", "iterations": 30000, "resolution": 2, "target_tris": 1_500_000, "min_component_frac": 0.002}
COME_ARGS = ("--splatting_config configs/hierarchical.json --use_ssimdecoupled_appearance "
             "--color_confidence --color_confidence_max 0.075 --color_confidence_from_iter 500 "
             "--lambda_variance 0.5 --lambda_normal_variance 0.005 --lambda_distortion 100 --far_plane 100. "
             "--detach_alpha False")


def run_come(ctx: StageContext, cfg: dict) -> Path:
    fr = ctx.fetch_stage("frames", "images")
    dp = ctx.work / "depth"
    data = ctx.work / "come_data"
    shutil.rmtree(data, ignore_errors=True)
    (data / "sparse").mkdir(parents=True)
    shutil.copytree(dp / "sparse", data / "sparse" / "0")
    # 3DGS-style loaders init from sparse/0/points3D.ply (else convert points3D.bin). The ARKit-fallback model has
    # no SfM points (empty init -> CUDA "invalid configuration argument"), so seed from the fused LiDAR cloud,
    # whose PLY already carries x/y/z, nx/ny/nz, red/green/blue by name.
    # (the CoMe image has no pycolmap: read the point count from the points3D.bin header, a uint64)
    pb = dp / "sparse" / "points3D.bin"
    n_pts = int(np.frombuffer(pb.read_bytes()[:8], np.uint64)[0]) if pb.exists() and pb.stat().st_size >= 8 else 0
    if n_pts < 1000 and (dp / "points.ply").exists():
        shutil.copyfile(dp / "points.ply", data / "sparse" / "0" / "points3D.ply")
    # 3DGS-style loaders open images/<basename(name)>; our model names are kf/<n>.jpg / hero/<n>.jpg -> flatten
    (data / "images").mkdir()
    for img in (fr / "images").rglob("*.jpg"):
        link = data / "images" / img.name
        if not link.exists():
            os.symlink(img, link)
    out = ctx.work / "come_out"
    shutil.rmtree(out, ignore_errors=True)
    it = cfg["iterations"]
    sh(f"python train.py -s {data} -m {out} -r {cfg['resolution']} --iterations {it} {COME_ARGS}",
       cwd=COME_DIR, log_file=ctx.out / "come_train.log")
    ctx.progress(0.7, "CoMe trained; extracting mesh (marching tetrahedra)")
    sh(f"python extract_mesh_tets.py -m {out} --iteration {it} --data_device cpu", cwd=COME_DIR,
       log_file=ctx.out / "come_extract.log")
    cands = sorted((out / "test" / f"ours_{it}").glob("mesh*binary_search*.ply")) or sorted(out.rglob("mesh*.ply"))
    if not cands:
        raise RuntimeError("CoMe produced no mesh")
    return cands[-1]


def postprocess(ctx: StageContext, raw: Path, cfg: dict):
    import open3d as o3d
    import trimesh
    from scipy.spatial import cKDTree

    from .splat import load_ply_points

    dp = ctx.work / "depth"
    world = json.loads((dp / "world.json").read_text())
    lo = np.array(world["bounds"]["min"]) - 0.3
    hi = np.array(world["bounds"]["max"]) + 0.3
    m = o3d.io.read_triangle_mesh(str(raw))
    m.remove_duplicated_vertices()
    m.remove_degenerate_triangles()
    m = m.crop(o3d.geometry.AxisAlignedBoundingBox(lo, hi))
    tri_clusters, n_tris, _ = m.cluster_connected_triangles()
    tri_clusters = np.asarray(tri_clusters)
    n_tris = np.asarray(n_tris)
    keep = n_tris[tri_clusters] >= cfg["min_component_frac"] * len(tri_clusters)
    m.remove_triangles_by_mask(~keep)
    m.remove_unreferenced_vertices()
    if len(m.triangles) > cfg["target_tris"]:
        m = m.simplify_quadric_decimation(cfg["target_tris"])
    m.compute_vertex_normals()
    V = np.asarray(m.vertices)
    pts, rgb = load_ply_points(dp / "points.ply")
    _, idx = cKDTree(pts).query(V, k=1)
    col = (rgb[idx] * 255).astype(np.uint8)
    o3d.io.write_triangle_mesh(str(ctx.out / "mesh.ply"), m)
    tm = trimesh.Trimesh(V, np.asarray(m.triangles), vertex_normals=np.asarray(m.vertex_normals),
                         vertex_colors=np.c_[col, np.full(len(col), 255, np.uint8)], process=False)
    raw_glb = ctx.work / "mesh_raw.glb"
    tm.export(raw_glb)
    try:
        sh(["gltfpack", "-i", str(raw_glb), "-o", str(ctx.out / "mesh.glb"), "-cc", "-kn"])
    except Exception:  # noqa: BLE001
        shutil.copyfile(raw_glb, ctx.out / "mesh.glb")
    return len(m.triangles), len(V)


def run(ctx: StageContext):
    cfg = {**DEFAULTS, **ctx.config.get("mesh", {})}
    ctx.fetch_stage("depth")
    engine = cfg["engine"]
    raw = None
    if engine == "come":
        try:
            raw = run_come(ctx, cfg)
        except Exception as e:  # noqa: BLE001
            ctx.metrics["come_error"] = str(e)[-500:]
            engine = "tsdf"
    if raw is None:
        raw = ctx.work / "depth" / "tsdf_mesh.ply"
        engine = "tsdf"
    ctx.progress(0.9, f"post-processing {engine} mesh")
    tris, verts = postprocess(ctx, raw, cfg)
    ctx.metrics.update({"engine": engine, "triangles": tris, "vertices": verts,
                        "glb_mb": round((ctx.out / "mesh.glb").stat().st_size / 1e6, 2)})
