"""Stage 1 — verify the uploaded capture and write a QA report."""
from __future__ import annotations

import json

import numpy as np

from ..capture import arkit_T, read_depth_bin, read_jsonl
from ..ctx import StageContext


def run(ctx: StageContext):
    m = ctx.manifest()
    if m.get("schema") != "capture_manifest.v1":
        raise ValueError(f"unsupported manifest schema {m.get('schema')}")
    missing, bad = [], []
    for f in m["files"]:
        info = ctx.store.crc32c(f"{ctx.raw_prefix}/{f['path']}")
        if info is None:
            missing.append(f["path"])
        elif info[0] != f["size"] or (f.get("crc32c") and info[1] != f["crc32c"]):
            bad.append({"path": f["path"], "expected": [f["size"], f.get("crc32c")], "got": list(info)})
    if missing or bad:
        raise ValueError(f"capture incomplete: missing={missing[:10]} bad={bad[:5]}")

    segs = []
    total_len, total_dur, n_frames, n_limited, n_depth = 0.0, 0.0, 0, 0, 0
    for s in m["segments"]:
        frames = read_jsonl(ctx.fetch_raw(s["frames"]))
        C = np.array([arkit_T(f)[:3, 3] for f in frames])
        seglen = float(np.linalg.norm(np.diff(C, axis=0), axis=1).sum()) if len(C) > 1 else 0.0
        lim = sum(1 for f in frames if f.get("track", "normal") != "normal")
        nd = sum(1 for _ in read_depth_bin(ctx.fetch_raw(s["depth"]), decode=False)) if s.get("depth") else 0
        dur = frames[-1]["t"] - frames[0]["t"] if frames else 0
        segs.append({"index": s["index"], "frames": len(frames), "path_m": round(seglen, 2), "duration_s": round(dur, 1),
                     "tracking_limited_frac": round(lim / max(1, len(frames)), 4), "depth_records": nd})
        total_len += seglen
        total_dur += dur
        n_frames += len(frames)
        n_limited += lim
        n_depth += nd
    qa = {"segments": segs, "frames": n_frames, "path_m": round(total_len, 2), "duration_s": round(total_dur, 1),
          "tracking_limited_frac": round(n_limited / max(1, n_frames), 4), "depth_records": n_depth,
          "device": m.get("device"), "video_format": m.get("video_format"), "capture_source": m.get("capture_source")}
    warnings = []
    if n_depth == 0:
        warnings.append("no LiDAR depth in capture")
    if qa["tracking_limited_frac"] > 0.2:
        warnings.append("ARKit tracking was limited for >20% of frames")
    qa["warnings"] = warnings
    (ctx.out / "qa.json").write_text(json.dumps(qa, indent=1))
    ctx.metrics.update({k: qa[k] for k in ["frames", "path_m", "duration_s", "depth_records", "tracking_limited_frac"]})
    ctx.status.update({"name": m.get("name") or ctx._doc.get("name"), "qa": qa})
