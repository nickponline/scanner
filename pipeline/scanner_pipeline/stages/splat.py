"""Stage 5 — Gaussian splat training with gsplat + MCMC (REQUIREMENTS §5.5).

- init from the LiDAR/TSDF point cloud (+ SfM points)
- L1 + D-SSIM, LiDAR log-depth supervision (consensus-filtered, from the depth stage)
- per-image affine colour correction (exposure / white balance), not baked into the export
- MCMC densification with a fixed Gaussian budget, antialiased rasterisation, SH degree 3
- every 8th keyframe held out for PSNR/SSIM

Outputs: scene.ply (standard 3DGS PLY), eval.json, eval/*.jpg
"""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path

import cv2
import numpy as np

from ..ctx import StageContext

# 90k steps, 2.5M cap at 1920 px (was 50k / 1.5M): whole-house scans (scene scale ~7) were under-fitted; ~3 h on an L4
DEFAULTS = {"steps": 90000, "cap_max": 2_500_000, "max_init": 400_000, "train_long_edge": 1920,
            "sh_degree": 3, "ssim_lambda": 0.2, "depth_lambda": 0.2, "depth_lambda_final": 0.05,
            "opacity_reg": 0.01, "scale_reg": 0.01, "app_reg": 1e-3, "app_gauge": 1.0, "holdout_every": 8, "seed": 0,
            "use_hero": True, "log_every": 250, "init_dense": True,
            # anisotropy regulariser (PhysGaussian-style): penalise max/min axis ratio above aniso_max -> fewer needle floaters
            "aniso_reg": 0.1, "aniso_max": 10.0, "eval_images": 10,
            # divergence guard: snapshot every N steps; roll back + damp LRs if the smoothed L1 blows up
            "guard_every": 1000, "guard_factor": 1.8, "guard_lr_damp": 0.5}

C0 = 0.28209479177387814


def load_ply_points(path: Path):
    with open(path, "rb") as f:
        hdr = b""
        while not hdr.endswith(b"end_header\n"):
            hdr += f.readline()
        n = int([l for l in hdr.decode().splitlines() if l.startswith("element vertex")][0].split()[-1])
        dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1"),
                       ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")])
        a = np.frombuffer(f.read(n * dt.itemsize), dt)
    return np.stack([a["x"], a["y"], a["z"]], 1), np.stack([a["r"], a["g"], a["b"]], 1) / 255.0


def ssim_fallback(x, y):
    import torch
    import torch.nn.functional as F

    C1, C2 = 0.01 ** 2, 0.03 ** 2
    g = torch.exp(-((torch.arange(11, device=x.device) - 5) ** 2) / (2 * 1.5 ** 2))
    g = (g / g.sum())
    w = (g[:, None] @ g[None, :])[None, None].repeat(3, 1, 1, 1)
    mu_x = F.conv2d(x, w, groups=3)
    mu_y = F.conv2d(y, w, groups=3)
    sxx = F.conv2d(x * x, w, groups=3) - mu_x ** 2
    syy = F.conv2d(y * y, w, groups=3) - mu_y ** 2
    sxy = F.conv2d(x * y, w, groups=3) - mu_x * mu_y
    s = ((2 * mu_x * mu_y + C1) * (2 * sxy + C2)) / ((mu_x ** 2 + mu_y ** 2 + C1) * (sxx + syy + C2))
    return s.mean()


def run(ctx: StageContext):
    if ctx.config.get("splat", {}).get("engine", "gsplat") == "spirula":
        from .splat_spirula import run as run_spirula

        return run_spirula(ctx)
    import pycolmap
    import torch
    import torch.nn.functional as F
    from gsplat import export_splats, rasterization
    from gsplat.strategy import MCMCStrategy

    try:
        from fused_ssim import fused_ssim

        def ssim(a, b):
            return fused_ssim(a, b, padding="valid")
    except ImportError:
        ssim = ssim_fallback

    cfg = {**DEFAULTS, **ctx.config.get("splat", {})}
    torch.manual_seed(cfg["seed"])
    random.seed(cfg["seed"])
    dev = "cuda"
    fr = ctx.fetch_stage("frames", "images")
    dp = ctx.fetch_stage("depth")
    rec = pycolmap.Reconstruction(str(dp / "sparse"))

    # ---------------- dataset
    views = []
    for im in sorted((i for i in rec.images.values() if i.has_pose), key=lambda i: i.name):
        if not cfg["use_hero"] and im.name.startswith("hero/"):
            continue
        cam = rec.cameras[im.camera_id]
        fx, fy, cx, cy = cam.params[:4]
        s = min(1.0, cfg["train_long_edge"] / max(cam.width, cam.height))
        W, H = int(round(cam.width * s)), int(round(cam.height * s))
        img = cv2.imread(str(fr / "images" / im.name))
        if img is None:
            continue
        if s < 1:
            img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
        img = torch.from_numpy(img[..., ::-1].copy()).pin_memory()
        K = torch.tensor([[fx * s, 0, (cx + 0.5) * s - 0.5], [0, fy * s, (cy + 0.5) * s - 0.5], [0, 0, 1]], dtype=torch.float32)
        w2c = torch.eye(4)
        w2c[:3] = torch.from_numpy(im.cam_from_world().matrix()).float()
        name = im.name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        dpath = dp / "depth" / f"{name}.npz"
        depth = torch.from_numpy(np.load(dpath)["depth"].astype(np.float32)) if dpath.exists() else None
        views.append({"name": name, "img": img, "K": K, "w2c": w2c, "W": W, "H": H, "depth": depth,
                      "kind": "hero" if im.name.startswith("hero/") else "keyframe"})
    kfs = [i for i, v in enumerate(views) if v["kind"] == "keyframe"]
    test_idx = set(kfs[cfg["holdout_every"] // 2::cfg["holdout_every"]])
    train = [i for i in range(len(views)) if i not in test_idx]
    test = sorted(test_idx)
    ctx.progress(0.02, f"{len(train)} train / {len(test)} test views")

    centers = torch.stack([torch.linalg.inv(v["w2c"])[:3, 3] for v in views])
    scene_scale = float((centers - centers.mean(0)).norm(dim=1).max()) * 1.1

    # ---------------- init
    # COLMAP points first (all of them, they are the best-registered geometry), LiDAR fills the remaining budget
    # (DA3 dense cloud from the densify stage replaces the LiDAR cloud when present: full coverage beyond LiDAR range)
    lp, lc = load_ply_points(dp / "points.ply")
    dense_src = "lidar"
    if cfg["init_dense"] and ctx.has_stage("densify"):
        try:
            dpts, dcol = load_ply_points(ctx.fetch_stage("densify", "dense.ply"))
            if len(dpts) > 10_000:
                lp, lc, dense_src = dpts, dcol, "da3"
        except Exception as e:  # noqa: BLE001
            ctx.metrics["init_dense_error"] = str(e)[:300]
    rng = np.random.default_rng(0)
    sp = np.zeros((0, 3))
    sc = np.zeros((0, 3))
    if rec.num_points3D() > 0:
        good = [p for p in rec.points3D.values() if p.error < 2.0 and p.track.length() >= 3]
        sp = np.array([p.xyz for p in good]).reshape(-1, 3)
        sc = (np.array([p.color for p in good]) / 255.0).reshape(-1, 3)
        if len(sp) > cfg["max_init"]:
            sel = rng.choice(len(sp), cfg["max_init"], replace=False)
            sp, sc = sp[sel], sc[sel]
    n_lidar = min(len(lp), cfg["max_init"] - len(sp))
    sel = rng.choice(len(lp), n_lidar, replace=False)
    pts, rgb = np.concatenate([sp, lp[sel]]), np.concatenate([sc, lc[sel]])
    init_counts = {"sfm": int(len(sp)), dense_src: int(n_lidar)}
    means = torch.tensor(pts, dtype=torch.float32, device=dev)
    from scipy.spatial import cKDTree

    dist, _ = cKDTree(pts).query(pts, k=4)
    d = np.sqrt((dist[:, 1:] ** 2).mean(1)).clip(1e-4, 0.5)
    N = len(pts)
    params = torch.nn.ParameterDict({
        "means": torch.nn.Parameter(means),
        "scales": torch.nn.Parameter(torch.log(torch.tensor(d * 0.5, dtype=torch.float32, device=dev))[:, None].repeat(1, 3)),
        "quats": torch.nn.Parameter(F.normalize(torch.rand(N, 4, device=dev), dim=-1)),
        "opacities": torch.nn.Parameter(torch.logit(torch.full((N,), 0.5, device=dev))),
        "sh0": torch.nn.Parameter(((torch.tensor(rgb, dtype=torch.float32, device=dev) - 0.5) / C0)[:, None, :]),
        "shN": torch.nn.Parameter(torch.zeros(N, (cfg["sh_degree"] + 1) ** 2 - 1, 3, device=dev)),
    })
    lrs = {"means": 1.6e-4 * scene_scale, "scales": 5e-3, "quats": 1e-3, "opacities": 5e-2, "sh0": 2.5e-3, "shN": 2.5e-3 / 20}
    opts = {k: torch.optim.Adam([{"params": params[k], "lr": lr, "name": k}], eps=1e-15) for k, lr in lrs.items()}

    def means_lr(step):  # exponential decay to 1% (replaces ExponentialLR so rollbacks can rebuild optimizers)
        return lrs["means"] * (0.01 ** (step / cfg["steps"])) * lr_mult
    app_A = torch.nn.Parameter(torch.eye(3, device=dev)[None].repeat(len(views), 1, 1))
    app_b = torch.nn.Parameter(torch.zeros(len(views), 3, device=dev))
    app_opt = torch.optim.Adam([app_A, app_b], lr=1e-3)
    strategy = MCMCStrategy(cap_max=cfg["cap_max"], refine_stop_iter=int(cfg["steps"] * 0.83))
    strategy.check_sanity(params, opts)
    sstate = strategy.initialize_state()

    def render(v, sh_deg, mode="RGB+ED"):
        colors = torch.cat([params["sh0"], params["shN"]], 1)
        out, alpha, info = rasterization(
            params["means"], params["quats"], torch.exp(params["scales"]), torch.sigmoid(params["opacities"]),
            colors, v["w2c"][None].to(dev), v["K"][None].to(dev), v["W"], v["H"], sh_degree=sh_deg,
            near_plane=0.01, far_plane=100.0, render_mode=mode, rasterize_mode="antialiased", packed=False)
        return out[0], alpha[0], info

    # ---------------- train
    t0 = time.time()
    order: list[int] = []
    curve: list[dict] = []
    dl_val = 0.0
    ema, best_ema, snap, rollbacks, skipped = None, float("inf"), None, [], 0
    lr_mult = 1.0

    def take_snapshot():
        return {k: p.detach().cpu().clone() for k, p in params.items()}, app_A.detach().cpu().clone(), app_b.detach().cpu().clone()

    def restore(sn):
        nonlocal opts
        pd, a, b = sn
        for k in params.keys():
            params[k] = torch.nn.Parameter(pd[k].to(dev))
        with torch.no_grad():
            app_A.copy_(a.to(dev))
            app_b.copy_(b.to(dev))
        opts = {k: torch.optim.Adam([{"params": params[k], "lr": lrs[k] * lr_mult, "name": k}], eps=1e-15) for k in lrs}

    for step in range(cfg["steps"]):
        if not order:
            order = train.copy()
            random.shuffle(order)
        vi = order.pop()
        for g in opts["means"].param_groups:
            g["lr"] = means_lr(step)
        v = views[vi]
        gt = v["img"].to(dev, non_blocking=True).float() / 255.0
        sh_deg = min(step // 1000, cfg["sh_degree"])
        out, alpha, info = render(v, sh_deg)
        rgb_r = out[..., :3]
        rgb_c = (rgb_r.reshape(-1, 3) @ app_A[vi].T + app_b[vi]).reshape(rgb_r.shape)
        l1 = (rgb_c - gt).abs().mean()
        ss = 1 - ssim(rgb_c.permute(2, 0, 1)[None], gt.permute(2, 0, 1)[None])
        loss = (1 - cfg["ssim_lambda"]) * l1 + cfg["ssim_lambda"] * ss
        if v["depth"] is not None:
            dgt = v["depth"].to(dev)
            dr = F.interpolate(out[..., 3][None, None], size=dgt.shape, mode="area")[0, 0]
            m = (dgt > 0) & (dr > 0.01)
            if m.sum() > 100:
                lam = cfg["depth_lambda"] + (cfg["depth_lambda_final"] - cfg["depth_lambda"]) * step / cfg["steps"]
                dl = (torch.log(dr[m]) - torch.log(dgt[m])).abs().mean()
                dl_val = float(dl)
                loss = loss + lam * dl
        loss = loss + cfg["opacity_reg"] * torch.sigmoid(params["opacities"]).mean() \
            + cfg["scale_reg"] * torch.exp(params["scales"]).mean() \
            + cfg["aniso_reg"] * F.relu(params["scales"].max(1).values - params["scales"].min(1).values
                                        - math.log(cfg["aniso_max"])).mean() \
            + cfg["app_reg"] * ((app_A[vi] - torch.eye(3, device=dev)).square().sum() + app_b[vi].square().sum()) \
            + cfg["app_gauge"] * ((app_A.mean(0) - torch.eye(3, device=dev)).square().sum() + app_b.mean(0).square().sum())
        # ^ gauge: the per-image corrections must average to identity, otherwise the base colours drift (auto-exposure
        #   video drove the mean gain to ~0.85, i.e. a splat ~15-20 % too bright in the viewer, where no correction applies)
        if not torch.isfinite(loss):
            skipped += 1
            for o in opts.values():
                o.zero_grad(set_to_none=True)
            app_opt.zero_grad(set_to_none=True)
            continue
        ema = l1.item() if ema is None else 0.98 * ema + 0.02 * l1.item()
        if step >= 2000 and step % 100 == 0 and snap is not None and ema > cfg["guard_factor"] * best_ema:
            lr_mult *= cfg["guard_lr_damp"]
            rollbacks.append({"step": step, "ema": round(ema, 4), "best": round(best_ema, 4), "lr_mult": lr_mult})
            restore(snap)
            sstate = strategy.initialize_state()
            ema = best_ema
            continue
        loss.backward()
        for o in opts.values():
            o.step()
            o.zero_grad(set_to_none=True)
        app_opt.step()
        app_opt.zero_grad(set_to_none=True)
        strategy.step_post_backward(params, opts, sstate, step, info, lr=means_lr(step))
        if step % cfg["guard_every"] == 0 and step >= 1000 and ema is not None and ema <= 1.2 * best_ema:
            best_ema = min(best_ema, ema)
            snap = take_snapshot()
        elif step >= 1000 and ema is not None and step % 100 == 0:
            best_ema = min(best_ema, ema) if ema <= 1.2 * best_ema else best_ema
        if step % cfg["log_every"] == 0:
            with torch.no_grad():
                op = torch.sigmoid(params["opacities"])
                curve.append({"step": step, "loss": round(loss.item(), 4), "l1": round(l1.item(), 4), "depth": round(dl_val, 4),
                              "op_mean": round(float(op.mean()), 4), "alive": int((op > 0.005).sum()),
                              "vis": int((info["radii"] > 0).sum()), "app_gain": round(float(app_A[vi].diagonal().mean()), 3),
                              "xyz_p99": round(float(params["means"].abs().quantile(0.99) if len(params["means"]) < 1.6e7 else 0), 2),
                              "view": v["name"]})
            if step % 1000 == 0:
                (ctx.out / "curve.json").write_text(json.dumps(curve))
        if step % 500 == 0:
            el = time.time() - t0
            ctx.progress(0.03 + 0.9 * step / cfg["steps"],
                         f"step {step}/{cfg['steps']} loss {loss.item():.4f} n={len(params['means'])} {el:.0f}s")

    # ---------------- eval
    (ctx.out / "eval").mkdir()
    psnrs, ssims, per_view, cc = [], [], [], []

    def psnr_of(vi):
        v = views[vi]
        out, _, _ = render(v, cfg["sh_degree"], mode="RGB")
        pred = out.clamp(0, 1)
        gt = v["img"].to(dev).float() / 255.0
        return pred, gt, float(-10 * torch.log10(F.mse_loss(pred, gt)))

    def psnr_cc(pred, gt):
        """PSNR after a least-squares per-image affine colour fit (exposure / white balance), as in NeRF-W style evals."""
        X = torch.cat([pred.reshape(-1, 3), torch.ones_like(pred.reshape(-1, 3)[:, :1])], 1)
        Y = gt.reshape(-1, 3)
        sel = torch.randperm(len(X), device=X.device)[:200_000]
        M = torch.linalg.lstsq(X[sel], Y[sel]).solution
        return float(-10 * torch.log10(F.mse_loss((X @ M).clamp(0, 1), Y)))

    with torch.no_grad():
        for k, vi in enumerate(test):
            pred, gt, ps = psnr_of(vi)
            ss_ = float(ssim_fallback(pred.permute(2, 0, 1)[None], gt.permute(2, 0, 1)[None]))
            pc = psnr_cc(pred, gt)
            psnrs.append(ps)
            ssims.append(ss_)
            cc.append(pc)
            per_view.append({"view": views[vi]["name"], "psnr": round(ps, 2), "psnr_cc": round(pc, 2), "ssim": round(ss_, 4)})
        # images: evenly spread over the test set, ground truth | render, max 1600 px wide
        pick = set(np.linspace(0, len(test) - 1, min(cfg["eval_images"], len(test))).round().astype(int).tolist()) if test else set()
        for k in sorted(pick):
            pred, gt, _ = psnr_of(test[k])
            cmp = (torch.cat([gt, pred], 1).cpu().numpy()[..., ::-1] * 255).astype(np.uint8)
            if cmp.shape[1] > 1600:
                cmp = cv2.resize(cmp, (1600, int(cmp.shape[0] * 1600 / cmp.shape[1])), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(ctx.out / "eval" / f"{views[test[k]]['name']}.jpg"), cmp, [cv2.IMWRITE_JPEG_QUALITY, 88])
        train_ps = [psnr_of(vi)[2] for vi in train[:: max(1, len(train) // 30)]]
    p = params
    export_splats(p["means"].detach(), p["scales"].detach(), p["quats"].detach(), p["opacities"].detach(),
                  p["sh0"].detach(), p["shN"].detach(), format="ply", save_to=str(ctx.out / "scene.ply"))
    ev = {"psnr": round(float(np.mean(psnrs)), 3) if psnrs else None, "ssim": round(float(np.mean(ssims)), 4) if ssims else None,
          "n_test": len(test), "n_train": len(train), "gaussians": len(p["means"]),
          "train_s": round(time.time() - t0, 1), "scene_scale": round(scene_scale, 3),
          "rollbacks": rollbacks, "skipped_steps": skipped, "init": init_counts,
          "train_psnr": round(float(np.mean(train_ps)), 3) if train_ps else None,
          "psnr_cc": round(float(np.mean(cc)), 3) if cc else None,
          "app_gain_mean": round(float(app_A.detach().diagonal(dim1=1, dim2=2).mean()), 4),
          "psnr_min": round(float(np.min(psnrs)), 2) if psnrs else None, "psnr_max": round(float(np.max(psnrs)), 2) if psnrs else None,
          "steps": cfg["steps"], "train_long_edge": cfg["train_long_edge"], "train_res": [views[0]["W"], views[0]["H"]] if views else None,
          "aniso_reg": cfg["aniso_reg"], "with_depth": sum(1 for v in views if v["depth"] is not None),
          "per_view": per_view}
    (ctx.out / "eval.json").write_text(json.dumps(ev, indent=1))
    (ctx.out / "curve.json").write_text(json.dumps(curve))
    ctx.metrics.update(ev)
    _ = math
