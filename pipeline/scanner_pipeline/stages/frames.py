"""Stage 2 — keyframe selection, image extraction, depth pairing.

Outputs (work/.../frames/):
  images/kf/{name}.jpg, images/hero/{name}.jpg
  depth/{name}.npz   (depth float16 metres, conf uint8)  — only for frames with LiDAR
  frames.json        [{name, kind, seg, i, t, w, h, K, T_arkit(row-major 4x4, GL cam / Y-up world), depth}]
"""
from __future__ import annotations

import json
import math

import av
import cv2
import numpy as np

from ..capture import arkit_T, read_depth_bin, read_jsonl
from ..ctx import StageContext
from ..geom import rotation_angle_deg

DEFAULTS = {"kf_dist_m": 0.20, "kf_rot_deg": 8.0, "max_keyframes": 1600, "sharp_window": 8, "jpeg_q": 95}


def select_keyframes(frames, sharp, has_depth, dist, rot, window):
    """Pose-gated keyframes; within ±window frames of each trigger pick the sharpest (prefer LiDAR frames)."""
    T = [arkit_T(f) for f in frames]
    picked = []
    last = None
    i = 0
    n = len(frames)
    while i < n:
        f = frames[i]
        if f.get("track", "normal").startswith("notAvailable"):
            i += 1
            continue
        if last is None or np.linalg.norm(T[i][:3, 3] - T[last][:3, 3]) > dist or \
                rotation_angle_deg(T[i][:3, :3], T[last][:3, :3]) > rot:
            lo, hi = max(0, i - window), min(n, i + window + 1)
            cand = [j for j in range(lo, hi) if j not in picked and (not picked or j > picked[-1])]
            score = [sharp[j] * (1.15 if has_depth[j] else 1.0) for j in cand]
            j = cand[int(np.argmax(score))]
            picked.append(j)
            last = j
            i = j + 1
            continue
        i += 1
    return picked


def run(ctx: StageContext):
    cfg = {**DEFAULTS, **ctx.config.get("frames", {})}
    m = ctx.manifest()
    out_frames = []
    (ctx.out / "images/kf").mkdir(parents=True)
    (ctx.out / "depth").mkdir(parents=True)
    nseg = len(m["segments"])
    for si, s in enumerate(m["segments"]):
        frames = read_jsonl(ctx.fetch_raw(s["frames"]))
        video = ctx.fetch_raw(s["video"])
        depth = {}
        if s.get("depth"):
            for r in read_depth_bin(ctx.fetch_raw(s["depth"])):
                depth[r.i] = r
        has_depth = [f["i"] in depth for f in frames]

        # pass 1: sharpness on downscaled grey
        sharp = np.zeros(len(frames))
        with av.open(str(video)) as cont:
            st = cont.streams.video[0]
            st.thread_type = "AUTO"
            for k, fr in enumerate(cont.decode(st)):
                if k >= len(frames):
                    break
                g = fr.to_ndarray(format="gray")
                g = cv2.resize(g, (g.shape[1] // 4, g.shape[0] // 4), interpolation=cv2.INTER_AREA)
                sharp[k] = cv2.Laplacian(g, cv2.CV_32F).var()
                if k % 300 == 0:
                    ctx.progress((si + 0.5 * k / len(frames)) / nseg, f"sharpness seg {si} frame {k}")

        dist, rot = cfg["kf_dist_m"], cfg["kf_rot_deg"]
        while True:
            kf = select_keyframes(frames, sharp, has_depth, dist, rot, cfg["sharp_window"])
            if len(kf) <= cfg["max_keyframes"] // nseg:
                break
            dist *= 1.15
            rot *= 1.15
        # drop extremely blurry picks (bottom 3% relative to segment median)
        med = float(np.median(sharp[kf])) if kf else 0
        kf = [j for j in kf if sharp[j] > 0.15 * med]
        want = set(kf)

        # pass 2: extract the selected frames
        with av.open(str(video)) as cont:
            st = cont.streams.video[0]
            st.thread_type = "AUTO"
            for k, fr in enumerate(cont.decode(st)):
                if k > max(kf):
                    break
                if k not in want:
                    continue
                f = frames[k]
                name = f"s{si:02d}_{k:06d}"
                img = fr.to_ndarray(format="bgr24")
                cv2.imwrite(str(ctx.out / "images/kf" / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, cfg["jpeg_q"]])
                rec = {"name": name, "kind": "keyframe", "seg": si, "i": k, "t": f["t"], "w": img.shape[1],
                       "h": img.shape[0], "K": f["K"], "T_arkit": arkit_T(f).tolist(), "sharpness": float(sharp[k]),
                       "depth": None}
                if k in depth:
                    d = depth[k]
                    np.savez_compressed(ctx.out / "depth" / f"{name}.npz", depth=d.depth.astype(np.float16), conf=d.conf)
                    rec["depth"] = f"depth/{name}.npz"
                out_frames.append(rec)
                ctx.progress((si + 0.5 + 0.5 * k / len(frames)) / nseg, f"extract seg {si} frame {k}")

    # hero stills
    n_hero = 0
    if m.get("stills"):
        (ctx.out / "images/hero").mkdir(parents=True)
        for st in read_jsonl(ctx.fetch_raw(m["stills"])):
            p = ctx.fetch_raw(st["file"])
            img = cv2.imread(str(p), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
            if img is None:
                continue
            name = "h_" + p.stem
            cv2.imwrite(str(ctx.out / "images/hero" / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
            out_frames.append({"name": name, "kind": "hero", "seg": st.get("seg", 0), "i": None, "t": st["t"],
                               "w": img.shape[1], "h": img.shape[0], "K": st["K"],
                               "T_arkit": arkit_T(st).tolist(), "sharpness": None, "depth": None})
            n_hero += 1

    (ctx.out / "frames.json").write_text(json.dumps({"frames": out_frames}, indent=0))
    n_kf = sum(1 for f in out_frames if f["kind"] == "keyframe")
    ctx.metrics.update({"keyframes": n_kf, "hero": n_hero,
                        "with_depth": sum(1 for f in out_frames if f["depth"]),
                        "kf_dist_m": round(dist, 3), "kf_rot_deg": round(rot, 2)})
    if n_kf < 20:
        raise ValueError(f"only {n_kf} keyframes — capture too short or tracking failed")
    _ = math
