"""Deterministic geometry for the floor-plan agent (REQUIREMENTS §5.8.1).

Argon reasons and labels; everything metric comes from here.
All coordinates are World (Z up, metres, floor z=0, walls roughly axis-aligned).
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy import ndimage


@dataclass
class Scene:
    pts: np.ndarray  # (N,3)
    rgb: np.ndarray  # (N,3) 0..1
    nrm: np.ndarray  # (N,3)
    ceiling: float
    cameras: list[dict]  # {id, center, c2w (4x4 OpenCV), image_path, w, h}
    lo: np.ndarray = field(init=False)
    hi: np.ndarray = field(init=False)

    def __post_init__(self):
        self.lo = np.percentile(self.pts[:, :2], 0.2, axis=0) - 0.3
        self.hi = np.percentile(self.pts[:, :2], 99.8, axis=0) + 0.3


# --------------------------------------------------------------------------- raster helpers
class Raster:
    """World XY <-> image pixel mapping (north-up image: +Y world is up)."""

    def __init__(self, lo, hi, px_per_m=None, max_px=1400, margin_px=60):
        self.lo, self.hi = np.asarray(lo, float), np.asarray(hi, float)
        ext = self.hi - self.lo
        self.s = px_per_m or min(100.0, (max_px - 2 * margin_px) / max(ext))
        self.m = margin_px
        self.W = int(math.ceil(ext[0] * self.s)) + 2 * margin_px
        self.H = int(math.ceil(ext[1] * self.s)) + 2 * margin_px

    def px(self, xy):
        xy = np.asarray(xy, float)
        u = (xy[..., 0] - self.lo[0]) * self.s + self.m
        v = (self.hi[1] - xy[..., 1]) * self.s + self.m
        return np.stack([u, v], -1)

    def grid(self, img, color=(150, 150, 150), label=True):
        step = 1.0 if (self.hi - self.lo).max() < 25 else 2.0
        for x in np.arange(math.ceil(self.lo[0] / step) * step, self.hi[0] + 1e-6, step):
            u = int(round(self.px([x, 0])[0]))
            cv2.line(img, (u, self.m), (u, self.H - self.m), color, 1)
            if label:
                cv2.putText(img, f"{x:g}", (u - 10, self.m - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
                cv2.putText(img, f"{x:g}", (u - 10, self.H - self.m + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
        for y in np.arange(math.ceil(self.lo[1] / step) * step, self.hi[1] + 1e-6, step):
            v = int(round(self.px([0, y])[1]))
            cv2.line(img, (self.m, v), (self.W - self.m, v), color, 1)
            if label:
                cv2.putText(img, f"{y:g}", (6, v + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
                cv2.putText(img, f"{y:g}", (self.W - self.m + 6, v + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.putText(img, "x (m) ->", (self.W // 2 - 30, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.putText(img, "y (m)", (4, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
        return img


def png_bytes(img_bgr) -> bytes:
    ok, buf = cv2.imencode(".png", img_bgr)
    return buf.tobytes()


def jpg_bytes(img_bgr, q=88) -> bytes:
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes()


# --------------------------------------------------------------------------- renders
def render_topdown(sc: Scene, z_min=0.0, z_max=None, bbox=None, mode="color", cameras=False):
    z_max = sc.ceiling - 0.15 if z_max is None else z_max
    lo, hi = (sc.lo, sc.hi) if bbox is None else (np.array(bbox[:2]), np.array(bbox[2:]))
    r = Raster(lo, hi)
    img = np.full((r.H, r.W, 3), 255, np.uint8)
    sel = (sc.pts[:, 2] >= z_min) & (sc.pts[:, 2] <= z_max)
    sel &= np.all((sc.pts[:, :2] >= lo) & (sc.pts[:, :2] <= hi), axis=1)
    P, C, N = sc.pts[sel], sc.rgb[sel], sc.nrm[sel]
    order = np.argsort(P[:, 2])  # painter: highest last
    P, C, N = P[order], C[order], N[order]
    uv = np.round(r.px(P[:, :2])).astype(int)
    ok = (uv[:, 0] >= 0) & (uv[:, 0] < r.W) & (uv[:, 1] >= 0) & (uv[:, 1] < r.H)
    uv, P, C, N = uv[ok], P[ok], C[ok], N[ok]
    if mode == "height":
        t = np.clip(P[:, 2] / max(z_max, 0.1), 0, 1)
        col = cv2.applyColorMap((t * 255).astype(np.uint8)[:, None], cv2.COLORMAP_TURBO)[:, 0]
    elif mode == "walls":
        col = np.where((np.abs(N[:, 2]) < 0.3)[:, None], np.array([[40, 40, 40]]), np.array([[225, 225, 225]])).astype(np.uint8)
    else:
        col = (C[:, ::-1] * 255).astype(np.uint8)
    rad = max(1, int(round(r.s * 0.012)))
    if rad <= 1:
        img[uv[:, 1], uv[:, 0]] = col
    else:
        for (u, v), c in zip(uv, col):
            cv2.circle(img, (int(u), int(v)), rad, tuple(int(x) for x in c), -1)
    r.grid(img)
    if cameras:
        for cam in sc.cameras:
            u, v = r.px(cam["center"][:2])
            cv2.circle(img, (int(u), int(v)), 3, (0, 0, 220), -1)
    return img, r


def wall_evidence(sc: Scene, res=0.02):
    """2D density of vertical surfaces between 0.4 m and ceiling-0.25 m."""
    zhi = min(2.1, sc.ceiling - 0.25)
    sel = (np.abs(sc.nrm[:, 2]) < 0.3) & (sc.pts[:, 2] > 0.4) & (sc.pts[:, 2] < zhi)
    P = sc.pts[sel]
    nx = int(math.ceil((sc.hi[0] - sc.lo[0]) / res))
    ny = int(math.ceil((sc.hi[1] - sc.lo[1]) / res))
    ix = np.clip(((P[:, 0] - sc.lo[0]) / res).astype(int), 0, nx - 1)
    iy = np.clip(((P[:, 1] - sc.lo[1]) / res).astype(int), 0, ny - 1)
    H = np.zeros((ny, nx), np.float32)
    np.add.at(H, (iy, ix), 1.0)
    H = ndimage.gaussian_filter(H, 1.0)
    return H, res


def wall_candidates(sc: Scene, min_len=0.35, gap=0.25):
    """Axis-aligned wall segments from the vertical-surface density (World is Manhattan-aligned)."""
    H, res = wall_evidence(sc)
    if H.max() <= 0:
        return []
    thr = np.percentile(H[H > 0], 70)
    B = H > thr
    out = []
    for axis in (0, 1):  # 0: walls running along x (constant y); 1: along y (constant x)
        M = B if axis == 0 else B.T
        Hm = H if axis == 0 else H.T
        rows = []
        for r_i in range(M.shape[0]):
            row = M[r_i]
            if row.sum() * res < min_len:
                continue
            idx = np.flatnonzero(row)
            segs = np.split(idx, np.flatnonzero(np.diff(idx) > gap / res) + 1)
            for sgm in segs:
                if len(sgm) * res >= min_len:
                    rows.append((r_i, sgm[0], sgm[-1], float(Hm[r_i, sgm[0]:sgm[-1] + 1].mean())))
        # merge adjacent rows (thick walls / both faces) into one line
        rows.sort()
        used = [False] * len(rows)
        for i, (r_i, a, b, w) in enumerate(rows):
            if used[i]:
                continue
            grp = [(r_i, a, b, w)]
            used[i] = True
            for j in range(i + 1, len(rows)):
                r2, a2, b2, w2 = rows[j]
                if r2 - grp[-1][0] > int(0.08 / res):
                    break
                if not used[j] and min(b, b2) - max(a, a2) > 0.5 * min(b - a, b2 - a2):
                    grp.append(rows[j])
                    used[j] = True
            rr = np.average([g[0] for g in grp], weights=[g[3] * (g[2] - g[1] + 1) for g in grp])
            a0, b0 = min(g[1] for g in grp), max(g[2] for g in grp)
            coord = (rr + 0.5) * res + (sc.lo[1] if axis == 0 else sc.lo[0])
            t0 = a0 * res + (sc.lo[0] if axis == 0 else sc.lo[1])
            t1 = (b0 + 1) * res + (sc.lo[0] if axis == 0 else sc.lo[1])
            if axis == 0:
                s, e = [t0, coord], [t1, coord]
            else:
                s, e = [coord, t0], [coord, t1]
            out.append({"start": [round(s[0], 3), round(s[1], 3)], "end": [round(e[0], 3), round(e[1], 3)],
                        "length": round(t1 - t0, 3), "support": round(float(np.mean([g[3] for g in grp])), 2),
                        "thickness_hint": round(max(0.08, (max(g[0] for g in grp) - min(g[0] for g in grp) + 1) * res), 3)})
    out.sort(key=lambda w: -w["length"] * w["support"])
    for i, w in enumerate(out):
        w["id"] = f"c{i + 1}"
    return out[:80]


def wall_support(sc: Scene, start, end, H_res=None, tol=0.06):
    H, res = H_res or wall_evidence(sc)
    s, e = np.asarray(start, float), np.asarray(end, float)
    L = np.linalg.norm(e - s)
    if L < 1e-3:
        return 0.0
    n = max(2, int(L / 0.05))
    d = (e - s) / L
    nrm = np.array([-d[1], d[0]])
    hit = 0
    thr = np.percentile(H[H > 0], 60) if (H > 0).any() else 1
    for t in np.linspace(0, L, n):
        best = 0.0
        for o in np.linspace(-tol, tol, 5):
            p = s + d * t + nrm * o
            ix, iy = int((p[0] - sc.lo[0]) / res), int((p[1] - sc.lo[1]) / res)
            if 0 <= iy < H.shape[0] and 0 <= ix < H.shape[1]:
                best = max(best, H[iy, ix])
        hit += best > thr
    return hit / n


def render_elevation(sc: Scene, start, end, depth=0.12, res=0.015):
    """Elevation view (looking at the wall from its left side) of points within ±depth of a wall line."""
    s, e = np.asarray(start, float), np.asarray(end, float)
    L = float(np.linalg.norm(e - s))
    d = (e - s) / max(L, 1e-6)
    nrm = np.array([-d[1], d[0]])
    rel = sc.pts[:, :2] - s
    t = rel @ d
    o = rel @ nrm
    zmax = sc.ceiling + 0.1
    sel = (np.abs(o) < depth) & (t > -0.2) & (t < L + 0.2) & (sc.pts[:, 2] > -0.05) & (sc.pts[:, 2] < zmax)
    W, H = int((L + 0.4) / res) + 1, int((zmax + 0.05) / res) + 1
    img = np.full((H, W, 3), 255, np.uint8)
    u = ((t[sel] + 0.2) / res).astype(int)
    v = (H - 1 - (sc.pts[sel, 2] + 0.05) / res).astype(int)
    ok = (u >= 0) & (u < W) & (v >= 0) & (v < H)
    img[v[ok], u[ok]] = (sc.rgb[sel][ok][:, ::-1] * 255).astype(np.uint8)
    scale = max(1, int(900 / max(W, H)))
    img = cv2.resize(img, (W * scale, H * scale), interpolation=cv2.INTER_NEAREST)
    pad = 40
    img = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    for k in np.arange(0, L + 1e-6, 0.5):
        uu = int(pad + (k + 0.2) / res * scale)
        cv2.line(img, (uu, pad), (uu, img.shape[0] - pad), (200, 200, 200) if k % 1 else (150, 150, 150), 1)
        if abs(k % 1) < 1e-6:
            cv2.putText(img, f"{k:g}", (uu - 6, img.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1)
    for zz in np.arange(0, zmax, 0.5):
        vv = int(pad + (H - 1 - (zz + 0.05) / res) * scale)
        cv2.line(img, (pad, vv), (img.shape[1] - pad, vv), (200, 200, 200), 1)
        cv2.putText(img, f"{zz:g}", (4, vv + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1)
    cv2.putText(img, "distance along wall from start (m); height z (m)", (pad, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
    return img


def object_candidates(sc: Scene, walls: list[dict], res=0.04, wall_clear=0.12):
    """Furniture-like clusters: occupied cells between 0.08 m and 2.0 m, away from wall lines."""
    sel = (sc.pts[:, 2] > 0.08) & (sc.pts[:, 2] < min(2.0, sc.ceiling - 0.3))
    P = sc.pts[sel]
    if len(P) == 0:
        return []
    keep = np.ones(len(P), bool)
    for w in walls:
        s, e = np.asarray(w["start"]), np.asarray(w["end"])
        L = np.linalg.norm(e - s)
        if L < 1e-3:
            continue
        d = (e - s) / L
        rel = P[:, :2] - s
        t = rel @ d
        o = np.abs(rel @ np.array([-d[1], d[0]]))
        keep &= ~((o < wall_clear) & (t > -0.05) & (t < L + 0.05))
    P = P[keep]
    nx = int(math.ceil((sc.hi[0] - sc.lo[0]) / res))
    ny = int(math.ceil((sc.hi[1] - sc.lo[1]) / res))
    ix = np.clip(((P[:, 0] - sc.lo[0]) / res).astype(int), 0, nx - 1)
    iy = np.clip(((P[:, 1] - sc.lo[1]) / res).astype(int), 0, ny - 1)
    cnt = np.zeros((ny, nx), np.int32)
    np.add.at(cnt, (iy, ix), 1)
    occ = ndimage.binary_closing(cnt >= 3, iterations=2)
    lab, n = ndimage.label(occ)
    out = []
    for k in range(1, n + 1):
        cells = np.argwhere(lab == k)
        area = len(cells) * res * res
        if area < 0.06:
            continue
        xy = np.c_[(cells[:, 1] + 0.5) * res + sc.lo[0], (cells[:, 0] + 0.5) * res + sc.lo[1]].astype(np.float32)
        (cx, cy), (w, h), ang = cv2.minAreaRect(xy)
        inb = (lab[iy, ix] == k)
        z = P[inb, 2]
        top = float(np.percentile(z, 97)) if len(z) else 0.5
        out.append({"center": [round(float(cx), 3), round(float(cy), 3), round(top / 2, 3)],
                    "size": [round(float(w), 3), round(float(h), 3), round(top, 3)], "yaw_deg": round(float(ang), 1),
                    "area_m2": round(area, 2)})
    out.sort(key=lambda o: -o["area_m2"])
    for i, o in enumerate(out):
        o["id"] = f"o{i + 1}"
    return out[:60]


def photos_near(sc: Scene, x, y, k=4):
    """Cameras ranked by how directly they look at (x, y) and how close they are."""
    p = np.array([x, y], float)
    scored = []
    for cam in sc.cameras:
        c = cam["center"][:2]
        fwd = cam["c2w"][:2, 2]
        v = p - c
        dist = np.linalg.norm(v)
        if dist < 1e-3:
            continue
        cosang = float(v @ fwd / (dist * max(np.linalg.norm(fwd), 1e-6)))
        if cosang < 0.5:
            continue
        scored.append((dist / (cosang + 1e-3), cam))
    scored.sort(key=lambda t: t[0])
    return [c for _, c in scored[:k]]


def ascii_table(rows: list[dict], keys: list[str]) -> str:
    buf = io.StringIO()
    buf.write(" | ".join(keys) + "\n")
    for r in rows:
        buf.write(" | ".join(str(r.get(k)) for k in keys) + "\n")
    return buf.getvalue()
