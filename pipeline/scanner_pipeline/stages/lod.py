"""Stage 6 — Spark LOD packaging (REQUIREMENTS §5.6): scene.ply -> scene.rad (bhatt-lod) + scene.spz."""
from __future__ import annotations

import json
import re
import shutil

from pathlib import Path

import numpy as np

from ..ctx import StageContext, sh


DEFAULTS = {"sh_iters": 3,  # CPU k-means over 65k centroids is ~7 min/iteration at 1.5M splats; 3 ~converges
            # floater cleanup before LOD: keep centres inside the TSDF room bounds + margin, drop huge / invisible.
            # The margin is deliberately wide: 3DGS models floor reflections as splats *below* the floor and the
            # view through windows as splats outside the walls. A 0.4 m crop cost 1.8 dB held-out PSNR (26.4 ->
            # 24.6) and 2 m still cost 0.4 dB; 6 m only drops far-off junk.
            "crop_margin_m": 6.0, "max_scale_m": 0.5, "min_opacity": 0.005,
            # needle floaters: long thin semi-transparent splats (blurred frames / weakly observed space)
            "needle_ratio": 20.0, "needle_max_opacity": 0.5, "needle_min_len_m": 0.05}


def read_gs_ply(path: Path):
    """3DGS PLY (all float32 properties) -> (header bytes, structured array)."""
    with open(path, "rb") as f:
        hdr = b""
        while not hdr.endswith(b"end_header\n"):
            hdr += f.readline()
        lines = hdr.decode().splitlines()
        n = int(next(l for l in lines if l.startswith("element vertex")).split()[-1])
        props = [l.split()[-1] for l in lines if l.startswith("property")]
        if any(l.split()[1] != "float" for l in lines if l.startswith("property")):
            raise ValueError("expected all-float 3DGS PLY")
        a = np.fromfile(f, np.dtype([(p, "<f4") for p in props]), count=n)
    return hdr, a


def clean_splats(src: Path, dst: Path, bounds: dict | None, cfg: dict) -> dict:
    hdr, a = read_gs_ply(src)
    n0 = len(a)
    reasons = {}
    keep = np.ones(n0, bool)
    if bounds:
        m = cfg["crop_margin_m"]
        lo, hi = np.array(bounds["min"]) - m, np.array(bounds["max"]) + m
        xyz = np.stack([a["x"], a["y"], a["z"]], 1)
        inside = np.all((xyz >= lo) & (xyz <= hi), axis=1)
        reasons["outside_bounds"] = int((~inside).sum())
        keep &= inside
    sc = np.exp(np.stack([a["scale_0"], a["scale_1"], a["scale_2"]], 1))
    smax, smin = sc.max(1), sc.min(1)
    op = 1 / (1 + np.exp(-a["opacity"]))
    huge = smax > cfg["max_scale_m"]
    faint = op < cfg["min_opacity"]
    # ratio of the two largest axes: a flat disc (surfel) is fine, a needle is not
    mid = np.sort(sc, 1)[:, 1]
    needle = (smax / np.maximum(mid, 1e-6) > cfg["needle_ratio"]) & (op < cfg["needle_max_opacity"]) & (smax > cfg["needle_min_len_m"])
    reasons.update({"too_large": int((keep & huge).sum()), "transparent": int((keep & ~huge & faint).sum()),
                    "needles": int((keep & ~huge & ~faint & needle).sum())})
    keep &= ~huge & ~faint & ~needle
    out = a[keep]
    hdr = re.sub(rb"element vertex \d+", b"element vertex %d" % len(out), hdr)
    with open(dst, "wb") as f:
        f.write(hdr)
        out.tofile(f)
    k = keep
    stats = {"scale_p50_m": round(float(np.median(smax[k])), 4) if k.any() else None,
             "scale_p99_m": round(float(np.percentile(smax[k], 99)), 4) if k.any() else None,
             "opacity_p50": round(float(np.median(op[k])), 3) if k.any() else None,
             "aniso_p99": round(float(np.percentile(smax[k] / np.maximum(smin[k], 1e-6), 99)), 1) if k.any() else None}
    return {"splats_in": n0, "splats_kept": int(len(out)), "cropped_frac": round(1 - len(out) / n0, 4),
            "removed": reasons, "kept_stats": stats, "bounds": bounds}


def run(ctx: StageContext):
    cfg = {**DEFAULTS, **ctx.config.get("lod", {})}
    sp = ctx.fetch_stage("splat", "scene.ply")
    work = ctx.work / "lod_build"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    src = work / "scene.ply"
    try:
        bounds = json.loads(ctx.fetch_stage("depth", "world.json").read_text()).get("bounds")
    except Exception:  # noqa: BLE001 - crop is an optimisation; LOD still works uncropped
        bounds = None
    clean = clean_splats(sp, src, bounds, cfg)
    ctx.progress(0.05, f"cleaned splats: kept {clean['splats_kept']}/{clean['splats_in']}")
    log = sh(["build-lod", "--quality", "--rad", f"--cluster-sh-cpu={cfg['sh_iters']}", str(src)], log_file=ctx.out / "build_lod.log")
    rad = work / "scene-lod.rad"
    # SPZ fallback = leaf level of the LOD tree (--unlod only accepts *-lod.* inputs). Optional.
    try:
        sh(["build-lod", "--spz", "--unlod", str(rad)], log_file=ctx.out / "build_spz.log")
        spz = next(p for p in work.glob("*.spz"))
        shutil.move(spz, ctx.out / "scene.spz")
    except (RuntimeError, StopIteration) as e:
        ctx.metrics["spz_error"] = str(e)[-300:]
    shutil.move(rad, ctx.out / "scene.rad")
    shutil.copyfile(src, ctx.out / "scene.ply")
    m = {}
    for k, pat in {"lod_levels": r"(?i)levels?[:= ]+(\d+)", "lod_nodes": r"(?i)nodes?[:= ]+(\d+)",
                   "splats": r"(?i)(\d+)\s+splats"}.items():
        hits = re.findall(pat, log)
        if hits:
            m[k] = int(hits[-1])
    m["rad_mb"] = round((ctx.out / "scene.rad").stat().st_size / 1e6, 2)
    if (ctx.out / "scene.spz").exists():
        m["spz_mb"] = round((ctx.out / "scene.spz").stat().st_size / 1e6, 2)
    ctx.metrics.update({**m, **clean})
