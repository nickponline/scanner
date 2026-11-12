"""Dense matching with RoMa v2 (Edstedt et al. 2025) into a COLMAP database.

Dense matching is far more robust than ALIKED/SIFT + LightGlue descriptor matching on the frames phones actually
produce indoors (motion blur, low light, blank walls). COLMAP needs one fixed keypoint set per image so tracks can chain
across many views; two modes:

- "keypoints" (default): keep the detector keypoints already in the database (ALIKED) and match them with RoMa's dense
  warp: warp A's keypoints into B, mutual nearest neighbours within `kp_max_px`. Tracks chain like a normal SfM run.
- "dense": sample RoMa correspondences and quantise them to a per-image grid (cell = `quant_px`); more matches per
  pair but short tracks, since every pair samples different pixels.

Matches are written raw and then geometrically verified by COLMAP (pycolmap.verify_matches).

Pairs: sequential neighbours (+ a few longer "quadratic" hops) plus spatial neighbours from the ARKit trajectory
(close and looking the same way), which is where loop closures come from.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

DEFAULTS = {"mode": "keypoints", "kp_max_px": 6.0, "setting": "precise", "num_matches": 5000, "min_overlap": 0.05, "quant_px": 4.0, "load_long_edge": 1920,
            "seq_overlap": 8, "seq_hops": (12, 16, 24, 32), "spatial_neighbors": 12, "spatial_max_dist": 3.0,
            "spatial_max_angle_deg": 75.0}


def make_pairs(frames: list[dict], centers: dict[str, np.ndarray], fwd: dict[str, np.ndarray], cfg: dict):
    """frames sorted by (seg, t); returns sorted unique (name_a, name_b) with name_a < name_b."""
    names = [f["img"] for f in frames]
    pairs = set()
    n = len(names)
    for i in range(n):
        for k in list(range(1, cfg["seq_overlap"] + 1)) + list(cfg["seq_hops"]):
            if i + k < n:
                pairs.add(tuple(sorted((names[i], names[i + k]))))
    C = np.array([centers[x] for x in names])
    Fw = np.array([fwd[x] for x in names])
    cos_max = np.cos(np.radians(cfg["spatial_max_angle_deg"]))
    for i in range(n):
        d = np.linalg.norm(C - C[i], axis=1)
        ok = (d <= cfg["spatial_max_dist"]) & (Fw @ Fw[i] >= cos_max)
        ok[i] = False
        for j in np.argsort(np.where(ok, d, np.inf))[: cfg["spatial_neighbors"]]:
            if ok[j]:
                pairs.add(tuple(sorted((names[i], names[j]))))
    return sorted(pairs)


class _Keypoints:
    """Per-image quantised keypoints: grid cell -> index, with running mean of member coordinates."""

    def __init__(self, q: float):
        self.q = q
        self.index: dict[tuple[int, int], int] = {}
        self.sum: list[list[float]] = []

    def ids(self, xy: np.ndarray) -> np.ndarray:
        cells = np.floor(xy / self.q).astype(np.int64)
        out = np.empty(len(xy), np.int64)
        for k, (c, p) in enumerate(zip(map(tuple, cells), xy)):
            i = self.index.get(c)
            if i is None:
                i = self.index[c] = len(self.sum)
                self.sum.append([0.0, 0.0, 0.0])
            s = self.sum[i]
            s[0] += p[0]
            s[1] += p[1]
            s[2] += 1
            out[k] = i
        return out

    def array(self) -> np.ndarray:
        if not self.sum:
            return np.zeros((0, 2), np.float32)
        s = np.asarray(self.sum)
        return (s[:, :2] / s[:, 2:3]).astype(np.float32)


def compute_matches(image_dir: Path, frames: list[dict], centers: dict, fwd: dict, cfg: dict, out_npz: Path,
                    log=print, progress=None, keypoints: dict[str, np.ndarray] | None = None) -> dict:
    """RoMa v2 over all pairs -> quantised keypoints + raw matches saved to `out_npz`. No pycolmap in this process
    (torch and pycolmap each bundle an OpenMP runtime; mixing them can segfault)."""
    import cv2
    import torch
    from romav2 import RoMaV2

    cfg = {**DEFAULTS, **cfg}
    t0 = time.time()
    frames = sorted(frames, key=lambda f: (f.get("seg", 0), f["t"]))
    pairs = make_pairs(frames, centers, fwd, cfg)
    log(f"[roma] {len(frames)} images, {len(pairs)} pairs, setting={cfg['setting']}")

    model = RoMaV2(RoMaV2.Cfg(setting=cfg["setting"]))
    dev = next(model.parameters()).device

    cache: dict[str, tuple] = {}

    def load(name: str):
        if name not in cache:
            im = cv2.imread(str(image_dir / name), cv2.IMREAD_COLOR)
            H, W = im.shape[:2]
            s = cfg["load_long_edge"] / max(H, W)
            if s < 1:
                im = cv2.resize(im, (round(W * s), round(H * s)), interpolation=cv2.INTER_AREA)
            t = torch.from_numpy(cv2.cvtColor(im, cv2.COLOR_BGR2RGB)).permute(2, 0, 1)[None].contiguous()
            cache[name] = (t, H, W)
        return cache[name]

    kp_mode = cfg["mode"] == "keypoints" and keypoints is not None
    kps = {f["img"]: _Keypoints(cfg["quant_px"]) for f in frames}
    raw: dict[tuple[str, str], np.ndarray] = {}
    for k, (a, b) in enumerate(pairs):
        ta, Ha, Wa = load(a)
        tb, Hb, Wb = load(b)
        preds = model.match(ta.to(dev), tb.to(dev))
        overlap = float(preds["overlap_AB"].mean())
        if kp_mode and overlap >= cfg["min_overlap"] and len(keypoints.get(a, [])) and len(keypoints.get(b, [])):
            mm = _match_keypoints(preds, keypoints[a], keypoints[b], (Ha, Wa), (Hb, Wb), cfg["kp_max_px"], dev)
            if len(mm) >= 30:
                raw[(a, b)] = mm
        elif not kp_mode and overlap >= cfg["min_overlap"]:
            m, conf, _, _ = model.sample(preds, cfg["num_matches"])
            if conf is not None:
                m = m[conf > 0.05]
            if len(m) >= 30:
                xa, xb = model.to_pixel_coordinates(m, Ha, Wa, Hb, Wb)
                xa, xb = xa.float().cpu().numpy(), xb.float().cpu().numpy()
                inb = ((xa[:, 0] >= 0) & (xa[:, 0] < Wa) & (xa[:, 1] >= 0) & (xa[:, 1] < Ha)
                       & (xb[:, 0] >= 0) & (xb[:, 0] < Wb) & (xb[:, 1] >= 0) & (xb[:, 1] < Hb))
                mm = np.stack([kps[a].ids(xa[inb]), kps[b].ids(xb[inb])], 1)
                # one-to-one: a cell may only match one cell in the other image
                mm = mm[np.unique(mm[:, 0], return_index=True)[1]]
                mm = mm[np.unique(mm[:, 1], return_index=True)[1]]
                raw[(a, b)] = mm.astype(np.uint32)
        if progress and k % 50 == 0:
            progress(k / len(pairs), f"RoMa v2 matching {k}/{len(pairs)}")
        if k % 200 == 0:
            log(f"[roma] {k}/{len(pairs)} pairs, overlap {overlap:.2f}, {len(raw.get((a, b), []))} matches, "
                f"{time.time() - t0:.0f}s")
        if len(cache) > 400:  # bounded memory on long scans: drop the oldest half
            for key in list(cache)[:200]:
                cache.pop(key)
    names = list(kps)
    pair_keys = list(raw)
    kp_arr = {n: (np.asarray(keypoints[n], np.float32) if kp_mode else kps[n].array()) for n in names}
    np.savez_compressed(
        out_npz, mode=np.array("keypoints" if kp_mode else "dense"), names=np.array(names), **{f"kp_{i}": kp_arr[n] for i, n in enumerate(names)},
        pairs=np.array(pair_keys).reshape(-1, 2), **{f"m_{i}": raw[p] for i, p in enumerate(pair_keys)})
    stats = {"pairs": len(pairs), "matched_pairs": len(raw), "match_seconds": round(time.time() - t0, 1),
             "mode": "keypoints" if kp_mode else "dense",
             "keypoints_mean": int(np.mean([len(v) for v in kp_arr.values()])) if kp_arr else 0,
             "matches_per_pair": int(np.mean([len(v) for v in raw.values()])) if raw else 0}
    log(f"[roma] matched {stats}")
    return stats


def _match_keypoints(preds, xa_px, xb_px, hw_a, hw_b, max_px: float, dev):
    """Mutual nearest neighbours between A's keypoints warped into B (RoMa warp_AB) and B's keypoints, gated by RoMa's
    overlap at A's keypoints and a pixel distance in B. Returns (N, 2) uint32 index pairs."""
    import torch
    import torch.nn.functional as F

    (Ha, Wa), (Hb, Wb) = hw_a, hw_b
    warp = preds["warp_AB"][0]  # (h, w, 2) in [-1, 1] of B
    ov = preds["overlap_AB"][0].reshape(warp.shape[0], warp.shape[1])
    xa = torch.as_tensor(np.asarray(xa_px, np.float32), device=dev)
    xb = torch.as_tensor(np.asarray(xb_px, np.float32), device=dev)
    na = torch.stack((xa[:, 0] / Wa * 2 - 1, xa[:, 1] / Ha * 2 - 1), -1)
    a2b = F.grid_sample(warp.permute(2, 0, 1)[None].float(), na[None, None], mode="bilinear", align_corners=False)[0, :, 0].T
    cert = F.grid_sample(ov[None, None].float(), na[None, None], mode="bilinear", align_corners=False)[0, 0, 0]
    a2b_px = torch.stack(((a2b[:, 0] + 1) / 2 * Wb, (a2b[:, 1] + 1) / 2 * Hb), -1)
    D = torch.cdist(a2b_px, xb)
    nn_ab = D.argmin(1)
    nn_ba = D.argmin(0)
    ia = torch.arange(len(xa), device=dev)
    ok = (nn_ba[nn_ab] == ia) & (D[ia, nn_ab] < max_px) & (cert > 0.3)
    return torch.stack((ia[ok], nn_ab[ok]), 1).cpu().numpy().astype(np.uint32)


def write_database(db_path: Path, npz: Path, max_error_px: float = 4.0) -> dict:
    """Replace keypoints/matches in `db_path` (images + cameras already present) with the RoMa result, then run
    COLMAP geometric verification on the matched pairs."""
    import pycolmap

    z = np.load(npz)
    names = [str(n) for n in z["names"]]
    pairs = [(str(a), str(b)) for a, b in z["pairs"]]
    db = pycolmap.Database.open(str(db_path))
    ids = {im.name: im.image_id for im in db.read_all_images()}
    db.clear_matches()
    db.clear_two_view_geometries()
    if ("mode" not in z.files) or str(z["mode"]) == "dense":
        db.clear_descriptors()
        db.clear_keypoints()
        for i, n in enumerate(names):
            if n in ids:
                db.write_keypoints(ids[n], z[f"kp_{i}"])
    for i, (a, b) in enumerate(pairs):
        if a in ids and b in ids:
            db.write_matches(ids[a], ids[b], z[f"m_{i}"])
    db.close()
    pairs_txt = db_path.parent / "roma_pairs.txt"
    pairs_txt.write_text("".join(f"{a} {b}\n" for a, b in pairs))
    opts = pycolmap.TwoViewGeometryOptions()
    opts.ransac.max_error = max_error_px  # full-res px; quantisation adds up to ~quant_px/2
    pycolmap.verify_matches(str(db_path), str(pairs_txt), opts)
    db = pycolmap.Database.open(str(db_path))
    stats = {"verified_pairs": db.num_verified_image_pairs(), "inlier_matches": int(db.num_inlier_matches())}
    db.close()
    return stats


def export_keypoints(db_path: Path, out_npz: Path) -> int:
    """Detector keypoints (x, y) per image name from a COLMAP database -> npz for the matching subprocess."""
    import pycolmap

    db = pycolmap.Database.open(str(db_path))
    names, arrs = [], {}
    for im in db.read_all_images():
        if db.exists_keypoints(im.image_id):
            arrs[f"kp_{len(names)}"] = np.asarray(db.read_keypoints(im.image_id))[:, :2].astype(np.float32)
            names.append(im.name)
    db.close()
    np.savez_compressed(out_npz, names=np.array(names), **arrs)
    return len(names)


def main():
    """python -m scanner_pipeline.roma_match job.json  (job: image_dir, frames, centers, fwd, cfg, out_npz)"""
    import json
    import sys

    job = json.loads(Path(sys.argv[1]).read_text())
    kp = None
    if job.get("keypoints_npz"):
        z = np.load(job["keypoints_npz"])
        kp = {str(n): z[f"kp_{i}"] for i, n in enumerate(z["names"])}
    st = compute_matches(Path(job["image_dir"]), job["frames"], {k: np.asarray(v) for k, v in job["centers"].items()},
                         {k: np.asarray(v) for k, v in job["fwd"].items()}, job["cfg"], Path(job["out_npz"]),
                         keypoints=kp)
    Path(job["out_npz"]).with_suffix(".json").write_text(json.dumps(st))


if __name__ == "__main__":
    main()
