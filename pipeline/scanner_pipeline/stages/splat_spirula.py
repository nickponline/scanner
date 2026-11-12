"""Splat stage, Spirula Studio engine (config splat.engine = "spirula").

Spirula (github.com/harry7557558/spirula-studio, GPL-3.0, run as an external CLI) trains 3DGS with a per-image ISP
model (exposure, white balance, vignetting, response curve) and a blur-tolerant SSIM. On scan_01m70g294kf68hwcdr it
matched gsplat's colour-corrected held-out PSNR but scored +3.7 dB *raw* PSNR: its base colours stay true to the
photos, while gsplat's per-image affine leaks exposure into the splats (renders ~12-15 % dark, view-inconsistent).
Raw colour is what the viewer shows.

Same inputs, holdout split and evaluation as the gsplat engine (splat.py), so the two are directly comparable:
frames/images + depth/sparse poses, densify dense.ply seed, every 8th keyframe deregistered before training, test
views rendered with gsplat's rasteriser. Outputs: scene.ply, eval.json, eval/*.jpg, spirula/{config,scene_transform}.json
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

import cv2
import numpy as np

from ..ctx import StageContext
from .splat import C0, ssim_fallback

SPIRULA_BIN = os.environ.get("SPIRULA_BIN", "/opt/spirula/spirula")
DEFAULTS = {"quality": "high", "steps": None, "cap_max": 3_000_000, "train_resolution_divisor": 2,
            "holdout_every": 8, "use_hero": True, "init_dense": True, "eval_images": 10, "extra_args": []}
STEP_RE = re.compile(r"step\s+(\d+)/(\d+).*?psnr=([\d.]+)")


def read_ply(path):
    with open(path, "rb") as f:
        hdr = b""
        while not hdr.endswith(b"end_header\n"):
            hdr += f.readline()
        lines = hdr.decode().splitlines()
        n = int(next(l for l in lines if l.startswith("element vertex")).split()[-1])
        props = [l.split()[-1] for l in lines if l.startswith("property")]
        a = np.fromfile(f, np.dtype([(p, "<f4") for p in props]), count=n)
    return a, props


def to_world(a, props, tf_path):
    """Undo Spirula's train_from_world normalisation (translation + uniform scale). Rotations would need SH rotation."""
    if not tf_path.exists():
        return a
    tf = json.loads(tf_path.read_text())["world_from_train"]
    R = np.array(tf["rotation"]["matrix_3x3"], float)
    s, t = float(tf["scale"]), np.array(tf["translation"], float)
    if not np.allclose(R, np.eye(3), atol=1e-6):
        raise RuntimeError(f"spirula applied a rotation to the scene ({R.tolist()}); SH rotation not implemented")
    if s == 1 and not t.any():
        return a
    a = a.copy()
    for i, k in enumerate("xyz"):
        a[k] = s * a[k] + t[i]
    for k in props:
        if k.startswith("scale_"):
            a[k] += np.float32(np.log(s))
    return a


def prepare(ctx: StageContext, cfg: dict):
    import pycolmap

    fr = ctx.fetch_stage("frames", "images")
    dp = ctx.fetch_stage("depth")
    rec = pycolmap.Reconstruction(str(dp / "sparse"))
    posed = sorted((i for i in rec.images.values() if i.has_pose), key=lambda i: i.name)
    if not cfg["use_hero"]:
        for im in posed:
            if im.name.startswith("hero/"):
                rec.deregister_frame(im.frame_id)
        posed = [i for i in posed if not i.name.startswith("hero/")]
    kfs = [i for i in posed if not i.name.startswith("hero/")]
    test = kfs[cfg["holdout_every"] // 2::cfg["holdout_every"]]
    for im in test:
        rec.deregister_frame(im.frame_id)
    data = ctx.work / "spirula_data"
    shutil.rmtree(data, ignore_errors=True)
    (data / "sparse" / "0").mkdir(parents=True)
    rec.write(str(data / "sparse" / "0"))
    os.symlink(fr / "images", data / "images")  # fetch_stage returns the frames dir
    seed = None
    if cfg["init_dense"] and ctx.has_stage("densify"):
        try:
            seed = ctx.fetch_stage("densify", "dense.ply")
        except Exception as e:  # noqa: BLE001
            ctx.metrics["init_dense_error"] = str(e)[:300]
    full = pycolmap.Reconstruction(str(dp / "sparse"))
    return data, seed, full, fr, dp, [im.name for im in test], len(posed) - len(test)


def run(ctx: StageContext):
    import torch
    import torch.nn.functional as F
    from gsplat import rasterization

    from .splat import DEFAULTS as GS_DEFAULTS

    cfg = {**DEFAULTS, **{k: v for k, v in ctx.config.get("splat", {}).items() if k in DEFAULTS}}
    data, seed, rec, fr, dp, test_names, n_train = prepare(ctx, cfg)
    ctx.progress(0.02, f"spirula: {n_train} train / {len(test_names)} test views")

    out_prefix = ctx.work / "spirula_out"
    shutil.rmtree(out_prefix, ignore_errors=True)
    cmd = [SPIRULA_BIN, "train", "3dgs", "--data", str(data), "--data-format", "colmap", "--colmap-recon-dir", "sparse/0",
           "--image-dir", "images", "--train-resolution-divisor", str(cfg["train_resolution_divisor"]),
           "--quality", cfg["quality"], "--cap-max", str(cfg["cap_max"]), "--output-dir-prefix", str(out_prefix),
           "--output-dir-name", "run", "--disable-viewer", "1"]
    if seed is not None:
        cmd += ["--seed-pointcloud", str(seed)]
    if cfg["steps"]:
        cmd += ["--num-iterations", str(cfg["steps"])]
    cmd += [str(x) for x in cfg["extra_args"]]
    t0 = time.time()
    log_path = ctx.out / "spirula.log"
    last, tail = 0.0, []
    with open(log_path, "w") as log, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                                      bufsize=1) as p:
        for line in p.stdout:
            log.write(line)
            tail = (tail + [line.rstrip()])[-30:]
            m = STEP_RE.search(line)
            if m and time.time() - last > 30:
                last = time.time()
                s, n = int(m[1]), int(m[2])
                ctx.progress(0.03 + 0.9 * s / n, f"spirula step {s}/{n} train psnr {m[3]} {time.time() - t0:.0f}s")
        rc = p.wait()
    if rc != 0:
        raise RuntimeError(f"spirula exited {rc}:\n" + "\n".join(tail))
    train_s = time.time() - t0
    run_dir = out_prefix / "run"
    ckpts = sorted(run_dir.glob("step-*.ckpt"))
    if not ckpts or not (ckpts[-1] / "splat.ply").exists():
        raise RuntimeError(f"no spirula checkpoint in {run_dir}")
    (ctx.out / "spirula").mkdir()
    for n in ("config.json", "scene_transform.json"):
        if (run_dir / n).exists():
            shutil.copyfile(run_dir / n, ctx.out / "spirula" / n)

    # ---------------- world-frame PLY (drop Spirula's normals so the export matches gsplat's layout)
    a, props = read_ply(ckpts[-1] / "splat.ply")
    a = to_world(a, props, run_dir / "scene_transform.json")
    keep = [p for p in props if p not in ("nx", "ny", "nz")]
    a = a[keep]
    hdr = "ply\nformat binary_little_endian 1.0\nelement vertex %d\n" % len(a) + "".join(f"property float {p}\n" for p in keep) + "end_header\n"
    with open(ctx.out / "scene.ply", "wb") as f:
        f.write(hdr.encode())
        np.ascontiguousarray(a).tofile(f)

    # ---------------- eval: identical to the gsplat engine (gsplat rasteriser, same views, same metrics)
    dev = "cuda"
    rest = sorted((p for p in keep if p.startswith("f_rest_")), key=lambda s: int(s.split("_")[-1]))
    nrest = len(rest) // 3
    sh_deg = int(round((nrest + 1) ** 0.5)) - 1
    T = lambda x: torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32)).to(dev)  # noqa: E731
    means = T(np.stack([a["x"], a["y"], a["z"]], 1))
    quats = T(np.stack([a[f"rot_{i}"] for i in range(4)], 1))
    scales = torch.exp(T(np.stack([a[f"scale_{i}"] for i in range(3)], 1)))
    opac = torch.sigmoid(T(a["opacity"]))
    sh0 = T(np.stack([a[f"f_dc_{i}"] for i in range(3)], 1))[:, None]
    shN = T(np.stack([a[r] for r in rest], 1)).reshape(len(a), 3, nrest).transpose(1, 2)  # PLY stores channel-major
    colors = torch.cat([sh0, shN], 1)

    long_edge = GS_DEFAULTS["train_long_edge"]
    by_name = {im.name: im for im in rec.images.values()}

    def view(name):
        im = by_name[name]
        cam = rec.cameras[im.camera_id]
        fx, fy, cx, cy = cam.params[:4]
        s = min(1.0, long_edge / max(cam.width, cam.height))
        W, H = int(round(cam.width * s)), int(round(cam.height * s))
        img = cv2.imread(str(fr / "images" / name))
        if s < 1:
            img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
        K = torch.tensor([[fx * s, 0, (cx + 0.5) * s - 0.5], [0, fy * s, (cy + 0.5) * s - 0.5], [0, 0, 1]], dtype=torch.float32, device=dev)
        w2c = torch.eye(4, device=dev)
        w2c[:3] = torch.from_numpy(im.cam_from_world().matrix()).float().to(dev)
        gt = torch.from_numpy(img[..., ::-1].copy()).to(dev).float() / 255.0
        return K, w2c, W, H, gt

    def render(name):
        K, w2c, W, H, gt = view(name)
        out, _, _ = rasterization(means, quats, scales, opac, colors, w2c[None], K[None], W, H, sh_degree=sh_deg,
                                  near_plane=0.01, far_plane=100.0, rasterize_mode="antialiased", packed=False)
        return out[0].clamp(0, 1), gt

    def psnr_cc(pred, gt):
        X = torch.cat([pred.reshape(-1, 3), torch.ones_like(pred.reshape(-1, 3)[:, :1])], 1)
        Y = gt.reshape(-1, 3)
        sel = torch.randperm(len(X), device=X.device)[:200_000]
        M = torch.linalg.lstsq(X[sel], Y[sel]).solution
        return float(-10 * torch.log10(F.mse_loss((X @ M).clamp(0, 1), Y)))

    (ctx.out / "eval").mkdir()
    psnrs, ssims, cc, per_view = [], [], [], []
    pick = set(np.linspace(0, len(test_names) - 1, min(cfg["eval_images"], len(test_names))).round().astype(int).tolist()) if test_names else set()
    with torch.no_grad():
        for k, name in enumerate(test_names):
            pred, gt = render(name)
            ps = float(-10 * torch.log10(F.mse_loss(pred, gt)))
            ss_ = float(ssim_fallback(pred.permute(2, 0, 1)[None], gt.permute(2, 0, 1)[None]))
            pc = psnr_cc(pred, gt)
            psnrs.append(ps)
            ssims.append(ss_)
            cc.append(pc)
            stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            per_view.append({"view": stem, "psnr": round(ps, 2), "psnr_cc": round(pc, 2), "ssim": round(ss_, 4)})
            if k in pick:
                cmp = (torch.cat([gt, pred], 1).cpu().numpy()[..., ::-1] * 255).astype(np.uint8)
                if cmp.shape[1] > 1600:
                    cmp = cv2.resize(cmp, (1600, int(cmp.shape[0] * 1600 / cmp.shape[1])), interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(ctx.out / "eval" / f"{stem}.jpg"), cmp, [cv2.IMWRITE_JPEG_QUALITY, 88])
        test_set = set(test_names)
        train_names = sorted(n for n, im in by_name.items() if im.has_pose and n not in test_set
                             and (cfg["use_hero"] or not n.startswith("hero/")))
        train_ps = [float(-10 * torch.log10(F.mse_loss(*render(n)))) for n in train_names[:: max(1, len(train_names) // 30)]]
    step_m = re.findall(r"step\s+(\d+)/(\d+)", log_path.read_text())
    ev = {"engine": "spirula", "psnr": round(float(np.mean(psnrs)), 3) if psnrs else None,
          "ssim": round(float(np.mean(ssims)), 4) if ssims else None, "n_test": len(test_names), "n_train": n_train,
          "gaussians": int(len(a)), "train_s": round(train_s, 1),
          "seed": "densify" if seed is not None else "sfm", "train_psnr": round(float(np.mean(train_ps)), 3) if train_ps else None,
          "psnr_cc": round(float(np.mean(cc)), 3) if cc else None,
          "psnr_min": round(float(np.min(psnrs)), 2) if psnrs else None, "psnr_max": round(float(np.max(psnrs)), 2) if psnrs else None,
          "steps": int(step_m[-1][1]) if step_m else cfg["steps"], "quality": cfg["quality"],
          "train_resolution_divisor": cfg["train_resolution_divisor"], "sh_degree": sh_deg, "per_view": per_view}
    (ctx.out / "eval.json").write_text(json.dumps(ev, indent=1))
    ctx.metrics.update(ev)
    _ = C0
