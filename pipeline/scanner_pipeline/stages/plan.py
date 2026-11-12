"""Stage 8 — floor plan + architectural/furniture model via the Gemini 4 Argon agent loop (REQUIREMENTS §5.8).

Outputs: building_model.json, architecture.glb, furniture.glb, ceilings.glb, plan_{style}.svg, overlay.png,
         agent/ (every image Argon saw + transcript.json)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

from ..ctx import StageContext, now_iso, sh
from ..plan.agent import MODEL, PlanAgent
from ..plan.build import build_glbs, overlay, to_building_model
from ..plan.geometry import Scene
from .splat import load_ply_points

DEFAULTS = {"max_turns": 40, "budget_s": 1800, "max_points": 1_500_000, "styles": ["stylized", "technical", "mono"]}


def gemini_key() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    from google.cloud import secretmanager

    c = secretmanager.SecretManagerServiceClient()
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT", "scanner-514023")
    return c.access_secret_version(name=f"projects/{proj}/secrets/gemini-api-key/versions/latest").payload.data.decode()


def load_plan_svg():
    try:
        from .. import plan_svg  # copied into the image at build time

        return plan_svg
    except ImportError:
        p = Path(__file__).resolve().parents[3] / "viewer" / "scripts" / "plan_svg.py"
        spec = importlib.util.spec_from_file_location("plan_svg", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["plan_svg"] = mod
        spec.loader.exec_module(mod)
        return mod


def load_scene(ctx: StageContext, cfg) -> Scene:
    import pycolmap

    dp = ctx.fetch_stage("depth")
    fr = ctx.fetch_stage("frames", "images")
    world = json.loads((dp / "world.json").read_text())
    pts, rgb = load_ply_points(dp / "points.ply")
    # normals live in the same file
    with open(dp / "points.ply", "rb") as f:
        hdr = b""
        while not hdr.endswith(b"end_header\n"):
            hdr += f.readline()
        dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1"),
                       ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")])
        a = np.frombuffer(f.read(), dt)
    nrm = np.stack([a["nx"], a["ny"], a["nz"]], 1)
    if len(pts) > cfg["max_points"]:
        sel = np.random.default_rng(0).choice(len(pts), cfg["max_points"], replace=False)
        pts, rgb, nrm = pts[sel], rgb[sel], nrm[sel]
    rec = pycolmap.Reconstruction(str(dp / "sparse"))
    cams = []
    for im in sorted((i for i in rec.images.values() if i.has_pose), key=lambda i: i.name):
        M = np.vstack([im.cam_from_world().matrix(), [0, 0, 0, 1]])
        c2w = np.linalg.inv(M)
        cam = rec.cameras[im.camera_id]
        cams.append({"id": im.name.rsplit("/", 1)[-1].rsplit(".", 1)[0], "center": c2w[:3, 3], "c2w": c2w,
                     "image_path": fr / "images" / im.name, "w": cam.width, "h": cam.height})
    # thin the camera list for the agent (every ~0.5 m) — keeps find_photos useful and cheap
    thinned, last = [], None
    for c in cams:
        if last is None or np.linalg.norm(c["center"] - last) > 0.5:
            thinned.append(c)
            last = c["center"]
    ceiling = world["levels"].get("ceiling") or 2.5
    if ceiling < 2.0:  # older depth runs measured shelf undersides as the ceiling
        ceiling = 2.5
    return Scene(pts=pts, rgb=rgb, nrm=nrm, ceiling=float(ceiling), cameras=thinned), world


def run(ctx: StageContext):
    cfg = {**DEFAULTS, **ctx.config.get("plan", {})}
    sc, world = load_scene(ctx, cfg)
    ctx.progress(0.05, "agent starting")
    agent = PlanAgent(sc, gemini_key(), ctx.out, max_turns=cfg["max_turns"], budget_s=cfg["budget_s"])
    res = agent.run()
    model = res["model"]
    prov = {"model": MODEL, "iterations": res["turns"], "created_at": now_iso(), "notes": res.get("notes", ""),
            "usage": res["usage"], "warnings": res.get("warnings", [])}
    bm = to_building_model(model, sc, res["derived"], world.get("north_deg"), prov)
    (ctx.out / "building_model.json").write_text(json.dumps(bm, indent=1))
    cv2.imwrite(str(ctx.out / "overlay.png"), overlay(model, sc))

    for name, scene in build_glbs(bm).items():
        raw = ctx.work / f"{name}_raw.glb"
        if len(scene.geometry) == 0:
            continue
        scene.export(raw)
        try:
            sh(["gltfpack", "-i", str(raw), "-o", str(ctx.out / f"{name}.glb"), "-cc", "-kn", "-km"])
        except Exception:  # noqa: BLE001
            shutil.copyfile(raw, ctx.out / f"{name}.glb")

    psvg = load_plan_svg()
    for style in cfg["styles"]:
        (ctx.out / f"plan_{style}.svg").write_text(psvg.render_plan_svg(bm, style))

    st = bm["storeys"][0]
    ctx.metrics.update({"rooms": len(st["rooms"]), "walls": len(st["walls"]), "openings": len(st["openings"]),
                        "furniture": len(st["furniture"]), "total_area_m2": res["derived"].get("total_area_m2"),
                        "turns": res["turns"], "elapsed_s": res["elapsed_s"], "tokens": res["usage"],
                        "auto_submitted": res.get("notes") == "auto-submitted last valid proposal"})
