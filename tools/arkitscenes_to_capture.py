#!/usr/bin/env python3
"""Convert an ARKitScenes *raw* sequence into a Scanner capture package (CONTRACTS.md §2).

This lets the backend be tested end-to-end with a real iPad/iPhone LiDAR capture before the
app is on a device. Needs: <id>.mov, lowres_wide.traj, vga_wide_intrinsics/, lowres_depth/, confidence/,
and vga_wide/ (only the file names are used, as per-frame timestamps).

ARKitScenes poses: traj stores world->camera (axis-angle, t); inverse = OpenCV camera->world, world Z-up.
We convert to ARKit convention (GL camera, Y-up world) because that is what the phone writes.

usage: arkitscenes_to_capture.py SRC_DIR OUT_DIR [--name NAME]
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.spatial.transform import Rotation, Slerp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from scanner_pipeline.capture import encode_depth_record  # noqa: E402
from scanner_pipeline.crc import crc32c_b64  # noqa: E402
from scanner_pipeline.geom import GL_TO_CV, YUP_TO_ZUP  # noqa: E402

ZUP_TO_YUP = np.linalg.inv(YUP_TO_ZUP)


def ts_of(p: Path) -> float:
    return float(p.stem.split("_", 1)[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    src, out = a.src, a.out
    vid = src.name
    if out.exists():
        shutil.rmtree(out)
    for d in ["video", "frames", "depth"]:
        (out / d).mkdir(parents=True)

    # trajectory -> interpolator
    traj = np.array([list(map(float, l.split())) for l in open(src / "lowres_wide.traj") if l.strip()])
    tt = traj[:, 0]
    Rcw = Rotation.from_rotvec(traj[:, 1:4])
    tcw = traj[:, 4:7]
    Rwc = Rcw.inv()
    cwc = -Rwc.apply(tcw)  # camera centres in world
    slerp = Slerp(tt, Rwc)

    vga_ts = sorted(ts_of(p) for p in (src / "vga_wide").glob("*.png"))
    intr = {ts_of(p): p for p in (src / "vga_wide_intrinsics").glob("*.pincam")}
    intr_ts = np.array(sorted(intr))
    cap = cv2.VideoCapture(str(src / f"{vid}.mov"))
    n_video = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    depth_files = {ts_of(p): p for p in (src / "lowres_depth").glob("*.png")}
    depth_ts = np.array(sorted(depth_files))
    # Video frame timestamps. lowres_depth is 60 Hz, 1:1 with the .mov frames (vga_wide is only 30 Hz, so it
    # must NOT be indexed by video frame). Extrapolate at 1/fps for any trailing frames.
    if abs(len(depth_ts) - n_video) <= 2:
        frame_t = np.array([depth_ts[i] if i < len(depth_ts) else depth_ts[-1] + (i - len(depth_ts) + 1) / fps
                            for i in range(n_video)])
    else:
        dur = vga_ts[-1] - vga_ts[0]
        frame_t = vga_ts[0] + np.arange(n_video) * (dur / max(n_video - 1, 1))
    print(f"video {n_video} frames @ {fps:.2f} fps, depth {len(depth_ts)}, span {frame_t[-1] - frame_t[0]:.2f}s")
    # nearest video frame for each depth map
    depth_for_frame = {}
    for dt in depth_ts:
        i = int(np.argmin(np.abs(frame_t - dt)))
        if abs(frame_t[i] - dt) < 0.5 / fps + 1e-3:
            depth_for_frame[i] = dt

    lines = []
    with open(out / "depth/seg_000.bin", "wb") as fdepth:
        for i, t in enumerate(frame_t):
            tc = float(np.clip(t, tt[0], tt[-1]))
            Rw = slerp([tc]).as_matrix()[0]
            c = np.array([np.interp(tc, tt, cwc[:, k]) for k in range(3)])
            T_cv = np.eye(4)
            T_cv[:3, :3], T_cv[:3, 3] = Rw, c
            T_arkit = ZUP_TO_YUP @ T_cv @ GL_TO_CV  # GL cam in Y-up world
            k = intr_ts[np.argmin(np.abs(intr_ts - t))]
            w0, h0, fx, fy, cx, cy = map(float, intr[k].read_text().split())
            s = W / w0
            track = "normal" if tt[0] <= t <= tt[-1] else "limited:initializing"
            has_d = i in depth_for_frame
            lines.append({
                "i": i, "t": float(t), "pts": i / fps, "T": T_arkit.T.reshape(-1).tolist(),
                "K": [fx * s, fy * s, (cx + 0.5) * s - 0.5, (cy + 0.5) * s - 0.5], "w": W, "h": H,
                "track": track, "exp": None, "iso": None, "depth": has_d,
            })
            if has_d:
                dts = depth_for_frame[i]
                d = np.asarray(Image.open(depth_files[dts]), dtype=np.float32) / 1000.0
                cpath = src / "confidence" / depth_files[dts].name
                conf = np.asarray(Image.open(cpath), dtype=np.uint8) if cpath.exists() else np.full(d.shape, 2, np.uint8)
                fdepth.write(encode_depth_record(i, float(t), d, conf))
    with open(out / "frames/seg_000.jsonl", "w") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")
    shutil.copyfile(src / f"{vid}.mov", out / "video/seg_000.mov")

    files = []
    for p in sorted(out.rglob("*")):
        if p.is_file():
            files.append({"path": str(p.relative_to(out)), "size": p.stat().st_size, "crc32c": crc32c_b64(p.read_bytes())})
    manifest = {
        "schema": "capture_manifest.v1", "scan_id": None, "name": a.name or f"ARKitScenes {vid}",
        "created_at": datetime.now(timezone.utc).isoformat(), "capture_source": "arkitscenes",
        "device": {"model": "iPad (ARKitScenes)", "os": "", "app_version": "converter"},
        "video_format": {"w": W, "h": H, "fps": fps, "codec": "hevc"},
        "segments": [{"index": 0, "video": "video/seg_000.mov", "frames": "frames/seg_000.jsonl",
                      "depth": "depth/seg_000.bin", "imu": None, "n_frames": n_video,
                      "duration_s": float(frame_t[-1] - frame_t[0])}],
        "stills": None, "arkit_mesh": None, "files": files, "heading_deg_at_start": None,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"wrote {out}: {n_video} frames, {len(depth_for_frame)} depth records, {len(files)} files")


if __name__ == "__main__":
    main()
