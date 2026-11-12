"""Stage 3 — Structure from Motion with COLMAP 4.2 (REQUIREMENTS §5.3).

ALIKED + LightGlue features (SIFT fallback), sequential + spatial (ARKit-prior) matching,
ARKit position + gravity priors in the database, global mapper (fallback: incremental pose-prior
mapper), then Sim(3)-align to the ARKit trajectory so the model is metric, gravity-aligned, Z-up.

COLMAP is the source of truth; ARKit is only a sanity check. ARKit VIO drifts ~1% of distance walked, so the
agreement gate scales with path length (p95 <= max(max_p95_m, drift_frac * path)). Frames COLMAP could not register
get their ARKit pose corrected by the (time-interpolated) COLMAP-vs-ARKit offset of the nearest registered frames,
so the splat/depth stages still see every view. All candidate models are kept in sfm/candidates/ for diagnosis.

Outputs (work/.../sfm/): sparse/ (COLMAP binary), sfm.json (metrics, per-image ARKit residuals)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from ..ctx import StageContext, sh
from ..geom import arkit_to_zup_cv

# features: "loma" (LoMa: DaD keypoints + DeDoDe-G descriptors, matched by the LoMa-G matcher; COLMAP 4.2 native),
#           "roma" (RoMa v2 dense matching, quantised keypoints; see roma_match.py), "aliked" (+LightGlue), "sift"
DEFAULTS = {"features": "loma", "loma_matcher": "LOMA_G", "roma": {}, "max_features": 4096, "seq_overlap": 12, "spatial_neighbors": 40,
            "spatial_max_dist": 4.0, "prior_std": 0.15, "mapper": "global",
            # accept a COLMAP model registering >= min_registered of the frames (the rest are filled, see above);
            # a model registering >= good_registered stops the search (no second mapper run)
            "min_registered": 0.5, "good_registered": 0.9,
            # quality gate vs ARKit after Sim(3): drop frames off by > max(outlier_m, 2*gate), then require
            # p95 <= gate where gate = max(max_p95_m, drift_frac * ARKit path length)
            "outlier_m": 0.3, "max_p95_m": 0.15, "drift_frac": 0.01, "fill_unregistered": True}


def colmap(*args, log=None):
    return sh(["colmap", *map(str, args)], log_file=log)


def extract(db, images, names, K, cfg, log):
    lst = db.parent / f"list_{abs(hash(tuple(names))) % 10**8}.txt"
    lst.write_text("\n".join(names) + "\n")
    feat = {"aliked": "ALIKED_N16ROT", "roma": "ALIKED_N16ROT", "loma": "LOMA_B"}.get(cfg["features"], "SIFT")
    args = ["feature_extractor", "--database_path", db, "--image_path", images, "--image_list_path", lst,
            "--ImageReader.camera_model", "PINHOLE", "--ImageReader.single_camera", 1,
            "--ImageReader.camera_params", ",".join(f"{v:.4f}" for v in K),
            "--FeatureExtraction.type", feat, "--FeatureExtraction.use_gpu", 1]
    if feat == "SIFT":
        args += ["--SiftExtraction.max_num_features", cfg["max_features"]]
    elif feat == "LOMA_B":
        args += ["--LomaExtraction.max_num_features", cfg["max_features"]]
    else:
        args += ["--AlikedExtraction.max_num_features", cfg["max_features"]]
    colmap(*args, log=log)


def write_priors(db_path: Path, frames: list[dict], std: float):
    import pycolmap

    db = pycolmap.Database.open(str(db_path)) if hasattr(pycolmap.Database, "open") else pycolmap.Database(str(db_path))
    by_name = {f["img"]: f for f in frames}
    n = 0
    db.clear_pose_priors()
    for img in db.read_all_images():
        f = by_name.get(img.name)
        if f is None:
            continue
        T = arkit_to_zup_cv(np.asarray(f["T_arkit"]))
        Rcw = T[:3, :3].T
        prior = pycolmap.PosePrior(
            corr_data_id=img.data_id, position=T[:3, 3],
            position_covariance=np.eye(3) * std ** 2,
            coordinate_system=pycolmap.PosePriorCoordinateSystem.CARTESIAN,
            gravity=Rcw @ np.array([0.0, 0.0, -1.0]),
        )
        db.write_pose_prior(prior)
        n += 1
    db.close()
    return n


def best_model(root: Path):
    import pycolmap

    best, best_n = None, -1
    for d in sorted(root.glob("*")):
        if (d / "images.bin").exists() or (d / "images.txt").exists():
            r = pycolmap.Reconstruction(str(d))
            if r.num_reg_images() > best_n:
                best, best_n = r, r.num_reg_images()
    return best


def arkit_model(frames: list[dict], out: Path):
    """COLMAP text model directly from ARKit poses (no features). Fallback + local testing."""
    import pycolmap

    out.mkdir(parents=True, exist_ok=True)
    cams, cam_lines, img_lines = {}, [], []
    for f in frames:
        key = (f["kind"], f["w"], f["h"])
        if key not in cams:
            K = np.median(np.array([g["K"] for g in frames if (g["kind"], g["w"], g["h"]) == key]), axis=0)
            cams[key] = len(cams) + 1
            cam_lines.append(f"{cams[key]} PINHOLE {f['w']} {f['h']} {K[0]} {K[1]} {K[2]} {K[3]}")
        T = arkit_to_zup_cv(np.asarray(f["T_arkit"]))
        w2c = np.linalg.inv(T)
        q = pycolmap.Rotation3d(w2c[:3, :3]).quat  # xyzw
        t = w2c[:3, 3]
        img_lines.append(f"{len(img_lines) + 1} {q[3]} {q[0]} {q[1]} {q[2]} {t[0]} {t[1]} {t[2]} {cams[key]} {f['img']}\n")
    (out / "cameras.txt").write_text("\n".join(cam_lines) + "\n")
    (out / "images.txt").write_text("".join(l + "\n" for l in img_lines))
    (out / "points3D.txt").write_text("")
    return pycolmap.Reconstruction(str(out))


def residuals(rec, arkit_c):
    import numpy as np
    return {im.name: float(np.linalg.norm(im.projection_center() - arkit_c[im.name]))
            for im in rec.images.values() if im.has_pose and im.name in arkit_c}


def align_to_arkit(rec, arkit_c: dict, outlier_m: float, tol_m: float = 0.25):
    """Sim(3) rec -> ARKit centres (registered images only); deregister frames off by > outlier_m and re-align.
    Mutates rec. Returns (sim, residuals, n_dropped) or None."""
    import pycolmap
    names = [im.name for im in rec.images.values() if im.has_pose and im.name in arkit_c]
    if len(names) < 3:
        return None
    sim = pycolmap.align_reconstruction_to_locations(
        rec, names, np.array([arkit_c[n] for n in names]), 3, pycolmap.RANSACOptions(max_error=max(0.25, tol_m)))
    if sim is None:
        return None
    rec.transform(sim)
    res = residuals(rec, arkit_c)
    bad = {n for n, v in res.items() if v > outlier_m}
    if bad:
        for im in list(rec.images.values()):
            if im.name in bad and im.has_pose:
                rec.deregister_frame(im.frame_id)
        keep = [n for n in names if n not in bad]
        sim2 = pycolmap.align_reconstruction_to_locations(
            rec, keep, np.array([arkit_c[n] for n in keep]), 3, pycolmap.RANSACOptions(max_error=max(0.1, tol_m)))
        if sim2 is None:
            return None
        rec.transform(sim2)
        sim = sim2 * sim
        res = residuals(rec, arkit_c)
    return sim, res, len(bad)


def run_roma(ctx: StageContext, db: Path, images: Path, frames: list[dict], rcfg: dict, log) -> dict:
    """RoMa v2 matching in a subprocess (torch and pycolmap each bundle OpenMP), then import + verify in-process."""
    import sys

    from .. import roma_match

    T = {f["img"]: arkit_to_zup_cv(np.asarray(f["T_arkit"])) for f in frames}
    job_dir = db.parent
    if rcfg.get("mode", roma_match.DEFAULTS["mode"]) == "keypoints":
        roma_match.export_keypoints(db, job_dir / "kp.npz")
    job = {"image_dir": str(images), "frames": frames, "cfg": rcfg, "out_npz": str(job_dir / "roma.npz"),
           "centers": {k: v[:3, 3].tolist() for k, v in T.items()}, "fwd": {k: v[:3, 2].tolist() for k, v in T.items()},
           "keypoints_npz": str(job_dir / "kp.npz") if (job_dir / "kp.npz").exists() else None}
    (job_dir / "roma_job.json").write_text(json.dumps(job))
    ctx.progress(0.3, "RoMa v2 dense matching")
    sh([sys.executable, "-m", "scanner_pipeline.roma_match", str(job_dir / "roma_job.json")], log_file=log)
    st = json.loads((job_dir / "roma.json").read_text())
    st.update(roma_match.write_database(db, job_dir / "roma.npz", rcfg.get("max_error_px", 4.0)))
    ctx.progress(0.6, f"RoMa v2: {st['verified_pairs']} verified pairs")
    return st


def c2w_of(im) -> np.ndarray:
    return np.linalg.inv(np.vstack([im.cam_from_world().matrix(), [0, 0, 0, 1]]))


def fill_unregistered(rec, frames: list[dict]) -> int:
    """Give every frame COLMAP did not register a pose: its ARKit pose, corrected by the COLMAP-vs-ARKit offset
    (C = c2w_colmap @ inv(c2w_arkit)) interpolated in time between the nearest registered frames of its segment.
    Mutates rec (rec must already be Sim(3)-aligned to ARKit). Returns the number of frames filled."""
    import pycolmap
    from scipy.spatial.transform import Rotation, Slerp

    by_name = {im.name: im for im in rec.images.values()}
    ark = {f["img"]: arkit_to_zup_cv(np.asarray(f["T_arkit"])) for f in frames}
    reg = {n for n, im in by_name.items() if im.has_pose}
    cam_for = {}
    for n, im in by_name.items():
        cam_for.setdefault(n.split("/")[0], im.camera_id)
    next_id = max(by_name_im.image_id for by_name_im in rec.images.values()) + 1 if rec.images else 1
    n_filled = 0
    for seg in sorted({f.get("seg", 0) for f in frames}):
        fs = sorted((f for f in frames if f.get("seg", 0) == seg), key=lambda f: f["t"])
        anchors = [f for f in fs if f["img"] in reg]
        if not anchors:
            corr_at = lambda t: np.eye(4)  # noqa: E731
        else:
            ts = np.array([f["t"] for f in anchors], float)
            Cs = [c2w_of(by_name[f["img"]]) @ np.linalg.inv(ark[f["img"]]) for f in anchors]
            if len(anchors) == 1:
                corr_at = lambda t, C=Cs[0]: C  # noqa: E731
            else:
                # strictly increasing times for Slerp
                keep = np.r_[True, np.diff(ts) > 1e-6]
                ts, Cs = ts[keep], [c for c, k in zip(Cs, keep) if k]
                slerp = Slerp(ts, Rotation.from_matrix([c[:3, :3] for c in Cs]))
                tr = np.array([c[:3, 3] for c in Cs])

                def corr_at(t, ts=ts, slerp=slerp, tr=tr):
                    t = float(np.clip(t, ts[0], ts[-1]))
                    C = np.eye(4)
                    C[:3, :3] = slerp([t]).as_matrix()[0]
                    C[:3, 3] = [np.interp(t, ts, tr[:, k]) for k in range(3)]
                    return C
        for f in fs:
            if f["img"] in reg:
                continue
            c2w = corr_at(f["t"]) @ ark[f["img"]]
            w2c = np.linalg.inv(c2w)
            pose = pycolmap.Rigid3d(pycolmap.Rotation3d(w2c[:3, :3]), w2c[:3, 3])
            im = by_name.get(f["img"])
            if im is not None:  # in the model but deregistered (e.g. dropped as an outlier)
                rec.frames[im.frame_id].rig_from_world = pose
                rec.register_frame(im.frame_id)
            else:
                cam_id = cam_for.get(f["img"].split("/")[0])
                if cam_id is None:  # no camera of this kind in the model: add one from the frame intrinsics
                    fx, fy, cx, cy = f["K"]
                    cam_id = max(rec.cameras) + 1 if rec.cameras else 1
                    rec.add_camera_with_trivial_rig(pycolmap.Camera(model="PINHOLE", width=f["w"], height=f["h"],
                                                                    params=[fx, fy, cx, cy], camera_id=cam_id))
                    cam_for[f["img"].split("/")[0]] = cam_id
                rec.add_image_with_trivial_frame(pycolmap.Image(name=f["img"], camera_id=cam_id, image_id=next_id), pose)
                next_id += 1
            n_filled += 1
    return n_filled


def run(ctx: StageContext):
    import pycolmap

    cfg = {**DEFAULTS, **ctx.config.get("sfm", {})}
    fr = ctx.fetch_stage("frames")
    frames = json.loads((fr / "frames.json").read_text())["frames"]
    images = fr / "images"
    for f in frames:
        f["img"] = f"{'kf' if f['kind'] == 'keyframe' else 'hero'}/{f['name']}.jpg"
    if cfg["mapper"] == "arkit":
        rec = arkit_model(frames, ctx.work / "sfm_arkit")
        (ctx.out / "sparse").mkdir()
        rec.write(str(ctx.out / "sparse"))
        ctx.metrics.update({"images": len(frames), "registered": rec.num_reg_images(), "mapper": "arkit",
                            "registered_frac": 1.0})
        (ctx.out / "sfm.json").write_text(json.dumps({"metrics": ctx.metrics}, indent=1))
        return
    log = ctx.out / "colmap.log"
    db = ctx.work / "sfm_db" / "database.db"
    if db.parent.exists():
        shutil.rmtree(db.parent)
    db.parent.mkdir(parents=True)

    # Matched-database cache: feature extraction + LoMa/RoMa matching is the slow part (~1-2 h for a few hundred
    # 4K frames), so a retry or a mapper-only change must not redo it. Keyed on the frames output + match config.
    import hashlib
    ckey_src = json.dumps({"frames": ctx.work_prefix("frames"), "imgs": sorted(f["img"] for f in frames),
                           **{k: cfg[k] for k in ("features", "loma_matcher", "roma", "max_features", "seq_overlap",
                                                  "spatial_neighbors", "spatial_max_dist", "prior_std")}}, sort_keys=True)
    ckey = f"work/{ctx.scan_id}/sfm_cache/{hashlib.sha1(ckey_src.encode()).hexdigest()[:16]}"
    cached = None
    if cfg.get("cache", True) and ctx.store.exists(f"{ckey}/meta.json"):
        try:
            cached = ctx.store.read_json(f"{ckey}/meta.json")
            ctx.store.download(f"{ckey}/database.db", db)
            ctx.progress(0.6, f"reusing matched database ({cached['features']}/{cached['matcher']})")
        except Exception as e:  # noqa: BLE001
            ctx.metrics["cache_error"] = str(e)[-200:]
            cached = None
            db.unlink(missing_ok=True)

    def do_features(kind: str):
        for group in ("keyframe", "hero"):
            fs = [f for f in frames if f["kind"] == group]
            if fs:
                K = np.median(np.array([f["K"] for f in fs]), axis=0)
                extract(db, images, [f["img"] for f in fs], K, {**cfg, "features": kind}, log)

    if cached is not None:
        feat, mtype, n_priors = cached["features"], cached["matcher"], cached["pose_priors"]
        ctx.metrics.update({k: v for k, v in cached.items() if k in ("roma",)})
        ctx.metrics["match_cache"] = "hit"
    else:
        feat = cfg["features"]
        chain = {"loma": ["loma", "aliked", "sift"], "aliked": ["aliked", "sift"], "roma": ["roma"]}.get(feat, [feat])
        for i, kind in enumerate(chain):
            try:
                db.unlink(missing_ok=True)
                do_features(kind)
                feat = kind
                break
            except RuntimeError:
                ctx.metrics[f"{kind}_failed"] = True
                if i == len(chain) - 1:
                    raise
        ctx.progress(0.25, f"features ({feat})")
        n_priors = write_priors(db, frames, cfg["prior_std"])

        if feat == "roma":
            mtype = "ROMA_V2"
            ctx.metrics["roma"] = run_roma(ctx, db, images, frames, cfg["roma"], log)
        else:
            mtype = {"loma": cfg["loma_matcher"], "aliked": "ALIKED_LIGHTGLUE"}.get(feat, "SIFT_LIGHTGLUE")
            common = ["--database_path", db, "--FeatureMatching.type", mtype, "--FeatureMatching.use_gpu", 1]
            try:
                colmap("sequential_matcher", *common, "--SequentialMatching.overlap", cfg["seq_overlap"],
                       "--SequentialMatching.quadratic_overlap", 1, log=log)
            except RuntimeError:
                if mtype != "SIFT_LIGHTGLUE":
                    raise
                mtype = "SIFT_BRUTEFORCE"
                common[3] = mtype
                colmap("sequential_matcher", *common, "--SequentialMatching.overlap", cfg["seq_overlap"], log=log)
            ctx.progress(0.45, "sequential matching")
            colmap("spatial_matcher", *common, "--SpatialMatching.max_num_neighbors", cfg["spatial_neighbors"],
                   "--SpatialMatching.max_distance", cfg["spatial_max_dist"], log=log)
            ctx.progress(0.6, "spatial matching")
        try:
            ctx.store.upload(db, f"{ckey}/database.db")
            ctx.store.write_json(f"{ckey}/meta.json", {"features": feat, "matcher": mtype, "pose_priors": n_priors,
                                                       **({"roma": ctx.metrics["roma"]} if "roma" in ctx.metrics else {}),
                                                       "key": json.loads(ckey_src)})
            ctx.metrics["match_cache"] = "stored"
        except Exception as e:  # noqa: BLE001
            ctx.metrics["cache_error"] = str(e)[-200:]
    try:
        _db = pycolmap.Database.open(str(db))
        ctx.metrics["verified_pairs"] = int(_db.num_verified_image_pairs())
        ctx.metrics["inlier_matches"] = int(_db.num_inlier_matches())
        _db.close()
    except Exception:  # noqa: BLE001
        pass

    n_imgs = len(frames)
    by_img = {f["img"]: f for f in frames}
    arkit_c = {k: arkit_to_zup_cv(np.asarray(f["T_arkit"]))[:3, 3] for k, f in by_img.items()}
    kf = sorted((f for f in frames if f["kind"] == "keyframe"), key=lambda f: (f.get("seg", 0), f["t"]))
    path_m = float(sum(np.linalg.norm(arkit_c[b["img"]] - arkit_c[a["img"]])
                       for a, b in zip(kf, kf[1:]) if a.get("seg", 0) == b.get("seg", 0)))
    gate_p95 = max(cfg["max_p95_m"], cfg["drift_frac"] * path_m)
    outlier_m = max(cfg["outlier_m"], 2 * gate_p95)
    attempts = {}
    cands = []

    def gate(name, rec):
        if rec is None:
            attempts[name] = {"ok": False, "why": "no model"}
            return None
        a = align_to_arkit(rec, arkit_c, outlier_m, gate_p95)
        if a is None:
            attempts[name] = {"ok": False, "why": "alignment failed"}
            return None
        sim, res, n_out = a
        r = np.array(list(res.values()))
        info = {"registered": rec.num_reg_images(), "outliers_dropped": n_out,
                "p95_m": round(float(np.percentile(r, 95)), 4), "rms_m": round(float(np.sqrt((r ** 2).mean())), 4)}
        info["points3d"] = rec.num_points3D()
        info["ok"] = info["registered"] >= cfg["min_registered"] * n_imgs and info["p95_m"] <= gate_p95
        attempts[name] = info
        try:  # keep every candidate (aligned) for later diagnosis
            d = ctx.out / "candidates" / name
            d.mkdir(parents=True, exist_ok=True)
            rec.write(str(d))
        except Exception:  # noqa: BLE001
            pass
        if info["ok"]:
            cands.append((info["registered"], name, (rec, sim, res)))
        return (rec, sim, res) if info["ok"] else None

    chosen, mapper_used = None, None
    if cfg["mapper"] == "global":
        out = ctx.work / "sfm_global"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        try:
            colmap("global_mapper", "--database_path", db, "--image_path", images, "--output_path", out, log=log)
            chosen = gate("global", best_model(out))
        except RuntimeError as e:
            attempts["global"] = {"ok": False, "why": str(e)[-300:]}
        mapper_used = "global"
        if chosen is not None and attempts["global"]["registered"] < cfg["good_registered"] * n_imgs:
            chosen = None  # usable but partial: also try the incremental mapper and keep the better one
    if chosen is None:
        ctx.progress(0.7, "pose-prior incremental mapper")
        out = ctx.work / "sfm_inc"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        try:
            colmap("pose_prior_mapper", "--database_path", db, "--image_path", images, "--output_path", out,
                   "--overwrite_priors_covariance", 1, "--prior_position_std_x", cfg["prior_std"],
                   "--prior_position_std_y", cfg["prior_std"], "--prior_position_std_z", cfg["prior_std"], log=log)
            gate("pose_prior_incremental", best_model(out))
        except RuntimeError as e:
            attempts["pose_prior_incremental"] = {"ok": False, "why": str(e)[-300:]}
        if cands:
            _, mapper_used, chosen = max(cands, key=lambda c: c[0])
    n_filled, n_colmap = 0, 0
    if chosen is None:
        # Last resort: ARKit VIO poses (metric, drift-prone but never catastrophic) - flagged in metrics.
        rec = arkit_model(frames, ctx.work / "sfm_arkit")
        sim, res, mapper_used = pycolmap.Sim3d(), residuals(rec, arkit_c), "arkit_fallback"
    else:
        rec, sim, res = chosen
        n_colmap = rec.num_reg_images()
        if cfg["fill_unregistered"]:
            n_filled = fill_unregistered(rec, frames)
            res = residuals(rec, arkit_c)
    ctx.progress(0.85, f"mapped with {mapper_used}")
    r = np.array(list(res.values()))
    sparse = ctx.out / "sparse"
    sparse.mkdir()
    rec.write(str(sparse))
    ctx.metrics.update({
        "images": n_imgs, "registered": rec.num_reg_images(), "registered_frac": round(rec.num_reg_images() / n_imgs, 4),
        "points3d": rec.num_points3D(), "mean_reproj_px": round(float(rec.compute_mean_reprojection_error()), 3),
        "mean_track_len": round(float(rec.compute_mean_track_length()), 2), "mapper": mapper_used, "features": feat,
        "matcher": mtype, "pose_priors": n_priors, "sim3_scale": round(float(sim.scale), 4),
        "arkit_residual_rms_m": round(float(np.sqrt((r ** 2).mean())), 4), "arkit_residual_p95_m": round(float(np.percentile(r, 95)), 4),
        "attempts": attempts, "arkit_path_m": round(path_m, 2), "gate_p95_m": round(gate_p95, 3),
        "colmap_registered": n_colmap, "filled_from_arkit": n_filled,
    })
    (ctx.out / "sfm.json").write_text(json.dumps({"metrics": ctx.metrics, "arkit_residual_m": res}, indent=1))
    if rec.num_reg_images() < 0.5 * n_imgs:
        raise RuntimeError(f"only {rec.num_reg_images()}/{n_imgs} images registered")
