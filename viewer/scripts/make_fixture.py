"""Generate the viewer demo fixture: a procedural ~9 x 6 m apartment, conforming to CONTRACTS.md §4.

    cd viewer && uv run --with numpy --with pillow scripts/make_fixture.py [--out public/fixtures/demo] [--fast]

Everything is derived from one list of oriented boxes (walls split around openings, floors, ceilings,
doors, windows, furniture, wall art). From that list we emit:
  model/building_model.json, model/{architecture,furniture,ceilings}.glb, mesh/mesh.glb,
  splat/scene.ply (standard 3DGS ply), points/points.ply, cameras.json (+ ray-traced photos),
  trajectory.json, plan/plan_{stylized,technical,mono}.svg and scene.json.

Photos are rendered with a small numpy ray caster against the same boxes and the same colour
function used for the splats, so photo click-through alignment can be checked visually.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plan_svg import STYLES, polygon_area, render_plan_svg, wall_pieces  # noqa: E402

RNG = np.random.default_rng(7)
SH_C0 = 0.28209479177387814
LIGHT = np.array([0.35, 0.55, 0.75]) / np.linalg.norm([0.35, 0.55, 0.75])

# ----------------------------------------------------------------------------- building model

def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def building_model() -> dict:
    E, I = 0.2, 0.1
    walls = [
        dict(id="w_s", start=[-4.5, -3.0], end=[4.5, -3.0], thickness=E, exterior=True),
        dict(id="w_e", start=[4.5, -3.0], end=[4.5, 3.0], thickness=E, exterior=True),
        dict(id="w_n", start=[4.5, 3.0], end=[-4.5, 3.0], thickness=E, exterior=True),
        dict(id="w_w", start=[-4.5, 3.0], end=[-4.5, -3.0], thickness=E, exterior=True),
        dict(id="w_i1", start=[-4.5, 0.4], end=[4.5, 0.4], thickness=I, exterior=False),
        dict(id="w_i2", start=[0.0, -3.0], end=[0.0, 0.4], thickness=I, exterior=False),
        dict(id="w_i3", start=[-0.8, 0.4], end=[-0.8, 3.0], thickness=I, exterior=False),
        dict(id="w_i4", start=[2.2, 0.4], end=[2.2, 3.0], thickness=I, exterior=False),
    ]
    for w in walls:
        w.update(height=2.6, base_z=0.0)
    rooms = [
        dict(id="r1", name="Living Room", type="living_room", polygon=rect(-4.4, -2.9, -0.05, 0.35), ceiling_z=2.6),
        dict(id="r2", name="Kitchen", type="kitchen", polygon=rect(0.05, -2.9, 4.4, 0.35), ceiling_z=2.6),
        dict(id="r3", name="Bedroom", type="bedroom", polygon=rect(-4.4, 0.45, -0.85, 2.9), ceiling_z=2.52),
        dict(id="r4", name="Hall + Entrance", type="hall", polygon=rect(-0.75, 0.45, 2.15, 2.9), ceiling_z=2.45),
        dict(id="r5", name="Bathroom", type="bathroom", polygon=rect(2.25, 0.45, 4.4, 2.9), ceiling_z=2.45),
    ]
    for r in rooms:
        r["area_m2"] = round(polygon_area(r["polygon"]), 1)
    D = dict(type="door", height=2.03, sill_z=0.0)
    W = dict(type="window", height=1.3, sill_z=0.9)
    openings = [
        dict(id="d1", wall_id="w_n", center=[0.6, 3.0], width=0.9, swing=dict(hinge="start", side="left"), **D),
        dict(id="d2", wall_id="w_i3", center=[-0.8, 1.2], width=0.8, swing=dict(hinge="end", side="left"), **D),
        dict(id="d3", wall_id="w_i4", center=[2.2, 1.2], width=0.75, swing=dict(hinge="start", side="right"), **D),
        dict(id="d4", wall_id="w_i1", center=[1.1, 0.4], width=0.85, swing=dict(hinge="end", side="right"), **D),
        dict(id="o1", wall_id="w_i2", center=[0.0, -1.4], width=1.6, type="opening", height=2.2, sill_z=0.0),
        dict(id="win1", wall_id="w_s", center=[-2.3, -3.0], width=1.6, **W),
        dict(id="win2", wall_id="w_s", center=[2.6, -3.0], width=1.4, **W),
        dict(id="win3", wall_id="w_w", center=[-4.5, -1.3], width=1.2, **W),
        dict(id="win4", wall_id="w_w", center=[-4.5, 1.7], width=1.2, **W),
        dict(id="win5", wall_id="w_e", center=[4.5, -1.4], width=1.0, **W),
        dict(id="win6", wall_id="w_e", center=[4.5, 1.7], width=0.6, type="window", height=0.6, sill_z=1.5),
    ]

    def F(fid, label, room, c, s, yaw=0.0):
        return dict(id=fid, label=label, room_id=room, center=[c[0], c[1], s[2] / 2 if len(c) == 2 else c[2]],
                    size=list(s), yaw_deg=yaw)

    furniture = [
        F("f1", "sofa", "r1", (-2.4, -0.4), (2.1, 0.9, 0.84)),
        F("f2", "coffee_table", "r1", (-2.4, -1.5), (1.1, 0.6, 0.44)),
        F("f3", "tv_unit", "r1", (-2.4, -2.7), (1.6, 0.4, 0.5)),
        F("f4", "rug", "r1", (-2.4, -1.4), (2.6, 1.8, 0.012)),
        F("f5", "armchair", "r1", (-4.0, -1.9), (0.8, 0.8, 0.8), 20.0),
        F("f6", "bookshelf", "r1", (-0.25, -2.6), (0.35, 0.6, 1.8)),
        F("f7", "counter", "r2", (3.0, -2.6), (2.8, 0.6, 0.9)),
        F("f8", "counter", "r2", (4.1, -1.35), (0.6, 1.5, 0.9)),
        F("f9", "fridge", "r2", (4.05, -0.2), (0.7, 0.7, 1.9)),
        F("f10", "table", "r2", (1.6, -1.2), (1.1, 1.1, 0.76)),
        F("f11", "chair", "r2", (0.85, -1.2), (0.45, 0.45, 0.9)),
        F("f12", "chair", "r2", (2.35, -1.2), (0.45, 0.45, 0.9)),
        F("f13", "chair", "r2", (1.6, -0.45), (0.45, 0.45, 0.9)),
        F("f14", "chair", "r2", (1.6, -1.95), (0.45, 0.45, 0.9)),
        F("f15", "bed", "r3", (-3.35, 1.6), (2.0, 1.6, 0.6)),
        F("f16", "wardrobe", "r3", (-1.4, 2.6), (1.0, 0.6, 2.1)),
        F("f17", "desk", "r3", (-1.75, 0.8), (1.0, 0.55, 0.76)),
        F("f18", "nightstand", "r3", (-4.15, 0.65), (0.4, 0.4, 0.5)),
        F("f19", "cabinet", "r4", (1.7, 2.7), (0.8, 0.35, 1.0)),
        F("f20", "bathtub", "r5", (3.35, 2.5), (1.7, 0.75, 0.56)),
        F("f21", "toilet", "r5", (4.1, 1.05), (0.6, 0.4, 0.8)),
        F("f22", "sink", "r5", (3.3, 0.65), (0.6, 0.4, 0.9)),
    ]
    return {
        "schema": "building_model.v1", "units": "m", "up": "z", "north_deg": 12.0,
        "storeys": [{"id": "s0", "name": "Ground", "floor_z": 0.0, "ceiling_z": 2.52,
                     "rooms": rooms, "walls": walls, "openings": openings, "furniture": furniture}],
        "provenance": {"model": "procedural-fixture", "iterations": 0,
                       "created_at": "2026-10-28T16:02:00Z", "notes": "viewer/scripts/make_fixture.py"},
    }


# ----------------------------------------------------------------------------- boxes

def box(id, kind, group, node, c, size, yaw=0.0, **kw):
    return dict(id=id, kind=kind, group=group, node=node, c=np.array(c, float), h=np.array(size, float) / 2,
                yaw=math.radians(yaw), **kw)


def boxes_from_model(bm: dict) -> list[dict]:
    st = bm["storeys"][0]
    out = []
    ops = st["openings"]
    for w in st["walls"]:
        p0, d, n, length, solid, holes = wall_pieces(w, ops)
        t, H = w["thickness"], w["height"]
        yaw = math.degrees(math.atan2(d[1], d[0]))

        def piece(a, b, z0, z1, k):
            mid = (a + b) / 2
            c = (p0[0] + d[0] * mid, p0[1] + d[1] * mid, (z0 + z1) / 2)
            out.append(box(f"{w['id']}_{k}", "wall", "architecture", w["id"], c, (b - a, t, z1 - z0), yaw))

        for k, (a, b) in enumerate(solid):
            piece(a, b, 0.0, H, k)
        for k, (a, b, o) in enumerate(holes):
            top = o["sill_z"] + o["height"]
            if top < H - 0.01:
                piece(a, b, top, H, f"t{k}")
            if o["sill_z"] > 0.01:
                piece(a, b, 0.0, o["sill_z"], f"s{k}")
            mid = (a + b) / 2
            c = (p0[0] + d[0] * mid, p0[1] + d[1] * mid)
            if o["type"] == "window":
                out.append(box(o["id"], "window", "architecture", o["id"],
                               (c[0], c[1], o["sill_z"] + o["height"] / 2), (b - a, 0.03, o["height"]), yaw))
            elif o["type"] == "door":
                sw = o.get("swing") or {}
                hinge = (c[0] - d[0] * o["width"] / 2, c[1] - d[1] * o["width"] / 2) if sw.get("hinge", "start") == "start" \
                    else (c[0] + d[0] * o["width"] / 2, c[1] + d[1] * o["width"] / 2)
                sg = 1.0 if sw.get("side", "left") == "left" else -1.0
                leaf_dir = (n[0] * sg, n[1] * sg)
                lc = (hinge[0] + leaf_dir[0] * o["width"] / 2, hinge[1] + leaf_dir[1] * o["width"] / 2, o["height"] / 2)
                out.append(box(o["id"], "door", "architecture", o["id"], lc, (o["width"] - 0.02, 0.04, o["height"]),
                               math.degrees(math.atan2(leaf_dir[1], leaf_dir[0]))))
    for r in st["rooms"]:
        xs = [p[0] for p in r["polygon"]]
        ys = [p[1] for p in r["polygon"]]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        out.append(box(f"floor_{r['id']}", "floor", "architecture", f"floor_{r['id']}",
                       ((x0 + x1) / 2, (y0 + y1) / 2, -0.02), (x1 - x0 + 0.1, y1 - y0 + 0.1, 0.04), room=r))
        cz = r["ceiling_z"]
        out.append(box(f"ceiling_{r['id']}", "ceiling", "ceilings", f"ceiling_{r['id']}",
                       ((x0 + x1) / 2, (y0 + y1) / 2, cz + 0.02), (x1 - x0, y1 - y0, 0.04), room=r))
    for f in st["furniture"]:
        out.append(box(f["id"], "furniture", "furniture", f["id"], f["center"], f["size"], f["yaw_deg"], label=f["label"]))
    art = [((-2.4, 0.33, 1.65), (1.1, 0.03, 0.7), 0), ((-4.38, -0.6, 1.6), (0.03, 0.6, 0.8), 1),
           ((-0.066, 0.0, 1.5), (0.03, 0.4, 0.5), 2), ((-2.6, 2.88, 1.55), (0.8, 0.03, 0.6), 3),
           ((1.6, 0.47, 1.6), (0.6, 0.03, 0.45), 4), ((-0.73, 2.0, 1.55), (0.03, 0.5, 0.65), 5),
           ((2.8, -2.88, 1.75), (0.5, 0.03, 0.4), 6)]
    for i, (c, s, seed) in enumerate(art):
        out.append(box(f"art{i}", "art", "decor", f"art{i}", c, s, 0.0, seed=seed))
    return out


# ----------------------------------------------------------------------------- colour

WALL_COLORS = {"r1": (0.93, 0.90, 0.85), "r2": (0.95, 0.92, 0.82), "r3": (0.78, 0.83, 0.91),
               "r4": (0.90, 0.88, 0.86), "r5": (0.80, 0.90, 0.89)}
FURN = {"sofa": (0.33, 0.45, 0.70), "coffee_table": (0.45, 0.31, 0.21), "tv_unit": (0.22, 0.22, 0.25),
        "armchair": (0.80, 0.55, 0.30), "bookshelf": (0.55, 0.40, 0.28), "counter": (0.50, 0.30, 0.18),
        "fridge": (0.88, 0.89, 0.90), "table": (0.25, 0.42, 0.68), "chair": (0.45, 0.30, 0.20),
        "bed": (0.93, 0.93, 0.95), "wardrobe": (0.82, 0.78, 0.70), "desk": (0.70, 0.58, 0.42),
        "nightstand": (0.60, 0.45, 0.30), "cabinet": (0.90, 0.90, 0.88), "bathtub": (0.97, 0.97, 0.97),
        "toilet": (0.97, 0.97, 0.97), "sink": (0.95, 0.95, 0.96), "rug": (0.62, 0.18, 0.16)}


def room_at(rooms, p):
    """Return index into rooms (or -1) for points p (N,2+)."""
    idx = np.full(len(p), -1)
    for i, r in enumerate(rooms):
        xs = [q[0] for q in r["polygon"]]
        ys = [q[1] for q in r["polygon"]]
        m = (p[:, 0] >= min(xs)) & (p[:, 0] <= max(xs)) & (p[:, 1] >= min(ys)) & (p[:, 1] <= max(ys))
        idx[m & (idx < 0)] = i
    return idx


def to_local(b, p):
    c, s = math.cos(b["yaw"]), math.sin(b["yaw"])
    q = p - b["c"]
    return np.stack([c * q[:, 0] + s * q[:, 1], -s * q[:, 0] + c * q[:, 1], q[:, 2]], 1)


def smooth_var(p, amp=0.03):
    return amp * (np.sin(p[:, 0] * 2.1 + p[:, 1] * 1.3) * np.sin(p[:, 2] * 1.7 + p[:, 1] * 0.7))


def color_at(b, p, n, rooms):
    """Base colour (N,3) in 0..1 for surface points p with world normals n on box b. Lighting baked in."""
    N = len(p)
    kind = b["kind"]
    if kind == "wall":
        ri = room_at(rooms, p + n * 0.15)
        col = np.tile(np.array([0.82, 0.82, 0.82]), (N, 1))
        for i, r in enumerate(rooms):
            col[ri == i] = WALL_COLORS.get(r["id"], (0.9, 0.9, 0.9))
        col[p[:, 2] < 0.09] = (0.97, 0.97, 0.96)
        col = col + smooth_var(p)[:, None]
    elif kind == "floor":
        rt = b["room"]["type"]
        if rt in ("bathroom", "kitchen"):
            chk = (np.floor(p[:, 0] / 0.3) + np.floor(p[:, 1] / 0.3)) % 2
            base = np.array([0.86, 0.86, 0.84]) if rt == "bathroom" else np.array([0.78, 0.70, 0.60])
            col = base[None] - chk[:, None] * 0.07
        else:
            plank = np.floor(p[:, 1] / 0.18)
            h = np.sin(plank * 12.9898) * 43758.5453
            h = h - np.floor(h)
            off = np.floor(p[:, 0] / 1.2 + h * 3)
            h2 = np.sin(off * 78.233 + plank) * 43758.5453
            h2 = h2 - np.floor(h2)
            col = np.array([0.70, 0.52, 0.35])[None] * (0.88 + 0.2 * h2[:, None])
            col = col + 0.025 * np.sin(p[:, 0] * 23 + h * 9)[:, None]
    elif kind == "ceiling":
        col = np.tile(np.array([0.96, 0.96, 0.95]), (N, 1))
    elif kind == "door":
        col = np.tile(np.array([0.58, 0.40, 0.26]), (N, 1)) + smooth_var(p * 3, 0.03)[:, None]
    elif kind == "window":
        col = np.stack([0.70 + 0.1 * (p[:, 2] - 0.9), 0.83 + 0.05 * (p[:, 2] - 0.9), np.full(N, 0.96)], 1)
    elif kind == "art":
        lp = to_local(b, p)
        s = b["seed"]
        u = lp[:, 0] + lp[:, 1]
        v = lp[:, 2]
        col = np.stack([0.55 + 0.4 * np.sin(u * 7 + s), 0.5 + 0.4 * np.sin(v * 9 + s * 2.1 + u * 3),
                        0.5 + 0.4 * np.sin((u + v) * 5 + s * 3.7)], 1)
        edge = np.zeros(len(p), bool)
        for a in range(3):
            if b["h"][a] > 0.05:  # skip the thin (depth) axis
                edge |= np.abs(lp[:, a]) > b["h"][a] - 0.04
        col[edge] = (0.15, 0.12, 0.1)
    else:  # furniture
        label = b.get("label", "")
        col = np.tile(np.array(FURN.get(label, (0.7, 0.7, 0.7))), (N, 1))
        lp = to_local(b, p)
        if label == "rug":
            band = (np.abs(lp[:, 0]) > b["h"][0] - 0.2) | (np.abs(lp[:, 1]) > b["h"][1] - 0.2)
            col[band] = (0.86, 0.74, 0.52)
            col[~band] += 0.08 * np.sin(lp[~band, 0] * 9)[:, None] * np.array([1, 0.4, 0.6])
        elif label == "counter":
            col[n[:, 2] > 0.5] = (0.88, 0.87, 0.83)
        elif label == "bed":
            blanket = lp[:, 0] > -b["h"][0] + 0.55
            col[blanket] = (0.55, 0.62, 0.80)
        elif label == "table":
            col[n[:, 2] < 0.5] = (0.35, 0.28, 0.2)
        col = col + smooth_var(p * 2, 0.025)[:, None]
    shade = 0.82 + 0.18 * (n @ LIGHT)
    shade = shade * (0.9 + 0.1 * np.clip(p[:, 2] / 0.6, 0, 1))
    return np.clip(col * shade[:, None], 0, 1)


# ----------------------------------------------------------------------------- surface sampling

def face_samples(b, spacing, rooms):
    """Yield (points, normals, local_axis) for each face to keep, jittered grid with given spacing."""
    c, s = math.cos(b["yaw"]), math.sin(b["yaw"])
    R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    h = b["h"]
    for axis in range(3):
        for sign in (-1, 1):
            if b["kind"] == "floor" and not (axis == 2 and sign == 1):
                continue
            if b["kind"] == "ceiling" and not (axis == 2 and sign == -1):
                continue
            if axis == 2 and sign == -1 and b["c"][2] - h[2] < 0.01:
                continue  # resting on floor
            o1, o2 = [a for a in range(3) if a != axis]
            n1 = max(1, int(round(2 * h[o1] / spacing)))
            n2 = max(1, int(round(2 * h[o2] / spacing)))
            g1, g2 = np.meshgrid((np.arange(n1) + 0.5) / n1, (np.arange(n2) + 0.5) / n2, indexing="ij")
            g1 = g1.ravel() + (RNG.random(g1.size) - 0.5) / n1 * 0.9
            g2 = g2.ravel() + (RNG.random(g2.size) - 0.5) / n2 * 0.9
            loc = np.zeros((g1.size, 3))
            loc[:, axis] = sign * h[axis]
            loc[:, o1] = (g1 * 2 - 1) * h[o1]
            loc[:, o2] = (g2 * 2 - 1) * h[o2]
            nl = np.zeros(3)
            nl[axis] = sign
            p = loc @ R.T + b["c"]
            n = np.tile(R @ nl, (len(p), 1))
            if b["kind"] in ("wall", "window"):
                ri = room_at(rooms, p + n * 0.15)
                keep = ri >= 0
                if b["kind"] == "wall":
                    ceil = np.array([rooms[i]["ceiling_z"] if i >= 0 else 0 for i in ri])
                    keep &= p[:, 2] <= ceil + 0.01
                p, n = p[keep], n[keep]
            if len(p):
                yield p, n, axis, (2 * h[o1] / n1, 2 * h[o2] / n2), (o1, o2)


def total_area(boxes):
    a = 0.0
    for b in boxes:
        h = b["h"]
        a += 8 * (h[0] * h[1] + h[1] * h[2] + h[0] * h[2])
    return a


def sample_scene(boxes, rooms, target):
    spacing = math.sqrt(total_area(boxes) * 0.55 / target)
    P, Nn, C, S, Q = [], [], [], [], []
    for b in boxes:
        quat = np.array([math.cos(b["yaw"] / 2), 0, 0, math.sin(b["yaw"] / 2)])  # w x y z
        for p, n, axis, (d1, d2), (o1, o2) in face_samples(b, spacing, rooms):
            col = color_at(b, p, n, rooms)
            sc = np.zeros((len(p), 3))
            sc[:, axis] = 0.004
            sc[:, o1] = d1 * 0.7
            sc[:, o2] = d2 * 0.7
            P.append(p); Nn.append(n); C.append(col); S.append(sc); Q.append(np.tile(quat, (len(p), 1)))
    return [np.concatenate(x) for x in (P, Nn, C, S, Q)], spacing


def sky_blobs(bm, n_per=1500):
    st = bm["storeys"][0]
    walls = {w["id"]: w for w in st["walls"]}
    P, C, S = [], [], []
    for o in st["openings"]:
        if o["type"] != "window":
            continue
        w = walls[o["wall_id"]]
        d = np.array(w["end"], float) - np.array(w["start"], float)
        d /= np.linalg.norm(d)
        n = np.array([-d[1], d[0]])
        c = np.array(o["center"], float)
        if np.all(np.abs(c + n * 0.3) < [4.5, 3.0]):
            n = -n  # point outward
        k = n_per
        along = (RNG.random(k) - 0.5) * (o["width"] + 1.2)
        out = 0.2 + RNG.random(k) ** 1.5 * 0.9
        z = o["sill_z"] - 0.3 + RNG.random(k) * (o["height"] + 0.8)
        xy = c[None] + d[None] * along[:, None] + n[None] * out[:, None]
        P.append(np.column_stack([xy, z]))
        t = RNG.random(k)
        col = np.where(t[:, None] < 0.55, np.array([0.62, 0.78, 0.95]), np.where(t[:, None] < 0.8, np.array([0.93, 0.95, 0.97]), np.array([0.45, 0.6, 0.38])))
        C.append(col + (RNG.random((k, 3)) - 0.5) * 0.08)
        S.append(np.tile((0.025 + RNG.random(k) * 0.05)[:, None], (1, 3)))
    return np.concatenate(P), np.clip(np.concatenate(C), 0, 1), np.concatenate(S)


def write_3dgs_ply(path, xyz, normals, rgb, scales, quats, opacity):
    n = len(xyz)
    props = ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2", "opacity",
             "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]
    header = "ply\nformat binary_little_endian 1.0\nelement vertex %d\n" % n
    header += "".join(f"property float {p}\n" for p in props) + "end_header\n"
    dc = (rgb - 0.5) / SH_C0
    op = np.log(opacity / (1 - opacity))
    data = np.column_stack([xyz, normals, dc, op, np.log(np.maximum(scales, 1e-7)), quats]).astype("<f4")
    with open(path, "wb") as fh:
        fh.write(header.encode())
        fh.write(data.tobytes())


def write_points_ply(path, xyz, rgb, normals):
    n = len(xyz)
    header = ("ply\nformat binary_little_endian 1.0\nelement vertex %d\n" % n +
              "property float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\n"
              "property float nx\nproperty float ny\nproperty float nz\nend_header\n")
    dt = np.dtype([("p", "<f4", 3), ("c", "u1", 3), ("n", "<f4", 3)])
    arr = np.zeros(n, dt)
    arr["p"] = xyz
    arr["c"] = np.clip(rgb * 255 + 0.5, 0, 255).astype(np.uint8)
    arr["n"] = normals
    with open(path, "wb") as fh:
        fh.write(header.encode())
        fh.write(arr.tobytes())


# ----------------------------------------------------------------------------- GLB writer

def box_mesh(b):
    """24 verts / 12 tris box with flat normals in world coords."""
    c, s = math.cos(b["yaw"]), math.sin(b["yaw"])
    R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    h = b["h"]
    V, N, I = [], [], []
    for axis in range(3):
        for sign in (-1, 1):
            o1, o2 = [a for a in range(3) if a != axis]
            quad = []
            for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                q = np.zeros(3)
                q[axis] = sign * h[axis]
                q[o1] = u * h[o1]
                q[o2] = v * h[o2]
                quad.append(R @ q + b["c"])
            nl = np.zeros(3)
            nl[axis] = sign
            nw = R @ nl
            base = len(V)
            V += quad
            N += [nw] * 4
            tri = [(0, 1, 2), (0, 2, 3)]
            # ensure CCW wrt normal
            if np.dot(np.cross(quad[1] - quad[0], quad[2] - quad[0]), nw) < 0:
                tri = [(0, 2, 1), (0, 3, 2)]
            I += [base + i for t in tri for i in t]
    return np.array(V, np.float32), np.array(N, np.float32), np.array(I, np.uint32)


def write_glb(path, nodes, color=(0.92, 0.92, 0.92, 1.0)):
    """nodes: list of dict(name, V, N, I, colors?(n,3 float), extras?)."""
    bin_parts, views, accessors, meshes, gnodes = [], [], [], [], []
    offset = 0

    def add_view(arr, target=None):
        nonlocal offset
        data = arr.tobytes()
        pad = (-len(data)) % 4
        bin_parts.append(data + b"\0" * pad)
        v = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target:
            v["target"] = target
        views.append(v)
        offset += len(data) + pad
        return len(views) - 1

    def add_acc(arr, ctype, typ, minmax=False, normalized=False):
        acc = {"bufferView": add_view(arr, 34963 if typ == "SCALAR" else 34962), "componentType": ctype,
               "count": len(arr), "type": typ}
        if minmax:
            acc["min"] = arr.min(0).tolist()
            acc["max"] = arr.max(0).tolist()
        if normalized:
            acc["normalized"] = True
        accessors.append(acc)
        return len(accessors) - 1

    has_colors = any(nd.get("colors") is not None for nd in nodes)
    for nd in nodes:
        attrs = {"POSITION": add_acc(nd["V"], 5126, "VEC3", True), "NORMAL": add_acc(nd["N"], 5126, "VEC3")}
        if nd.get("colors") is not None:
            col = np.clip(nd["colors"] * 255 + 0.5, 0, 255).astype(np.uint8)
            col = np.column_stack([col, np.full(len(col), 255, np.uint8)])
            attrs["COLOR_0"] = add_acc(col, 5121, "VEC4", normalized=True)
        prim = {"attributes": attrs, "indices": add_acc(nd["I"], 5125, "SCALAR"), "material": 0}
        meshes.append({"name": nd["name"], "primitives": [prim]})
        node = {"name": nd["name"], "mesh": len(meshes) - 1}
        if nd.get("extras"):
            node["extras"] = nd["extras"]
        gnodes.append(node)
    gltf = {
        "asset": {"version": "2.0", "generator": "scanner make_fixture.py",
                  "extras": {"frame": "World (Z up, metres) per CONTRACTS.md §1"}},
        "scene": 0, "scenes": [{"nodes": list(range(len(gnodes)))}], "nodes": gnodes, "meshes": meshes,
        "materials": [{"name": "default", "pbrMetallicRoughness": {
            "baseColorFactor": [1, 1, 1, 1] if has_colors else list(color), "metallicFactor": 0, "roughnessFactor": 0.9}}],
        "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": offset}],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    binary = b"".join(bin_parts)
    total = 12 + 8 + len(js) + 8 + len(binary)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        fh.write(struct.pack("<II", len(binary), 0x004E4942) + binary)


def group_nodes(boxes, group, extras_fn=None):
    by = {}
    for b in boxes:
        if b["group"] != group:
            continue
        by.setdefault(b["node"], []).append(b)
    nodes = []
    for name, bs in by.items():
        Vs, Ns, Is, off = [], [], [], 0
        for b in bs:
            V, N, I = box_mesh(b)
            Vs.append(V); Ns.append(N); Is.append(I + off)
            off += len(V)
        nodes.append(dict(name=name, V=np.concatenate(Vs), N=np.concatenate(Ns), I=np.concatenate(Is),
                          extras=extras_fn(bs[0]) if extras_fn else {"kind": bs[0]["kind"]}))
    return nodes


def tess_mesh(boxes, rooms, step=0.12):
    """A 'reconstructed' mesh: all visible faces tessellated with baked vertex colours."""
    V, N, C, I = [], [], [], []
    off = 0
    for b in boxes:
        c, s = math.cos(b["yaw"]), math.sin(b["yaw"])
        R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        h = b["h"]
        for axis in range(3):
            for sign in (-1, 1):
                if b["kind"] == "floor" and not (axis == 2 and sign == 1):
                    continue
                if b["kind"] == "ceiling" and not (axis == 2 and sign == -1):
                    continue
                o1, o2 = [a for a in range(3) if a != axis]
                n1 = max(1, int(math.ceil(2 * h[o1] / step)))
                n2 = max(1, int(math.ceil(2 * h[o2] / step)))
                g1, g2 = np.meshgrid(np.linspace(-1, 1, n1 + 1), np.linspace(-1, 1, n2 + 1), indexing="ij")
                loc = np.zeros((g1.size, 3))
                loc[:, axis] = sign * h[axis]
                loc[:, o1] = g1.ravel() * h[o1]
                loc[:, o2] = g2.ravel() * h[o2]
                nl = np.zeros(3)
                nl[axis] = sign
                p = loc @ R.T + b["c"]
                nw = R @ nl
                nn = np.tile(nw, (len(p), 1))
                if b["kind"] in ("wall", "window") and room_at(rooms, (b["c"] + nw * (h[axis] + 0.15))[None])[0] < 0:
                    continue
                col = color_at(b, p, nn, rooms)
                ii = np.arange(g1.size).reshape(n1 + 1, n2 + 1)
                a0, a1, a2, a3 = ii[:-1, :-1].ravel(), ii[1:, :-1].ravel(), ii[1:, 1:].ravel(), ii[:-1, 1:].ravel()
                tris = np.column_stack([a0, a1, a2, a0, a2, a3]).reshape(-1, 3)
                q = p[tris]
                if np.dot(np.cross(q[0, 1] - q[0, 0], q[0, 2] - q[0, 0]), nw) < 0:
                    tris = tris[:, ::-1]
                V.append(p); N.append(nn); C.append(col); I.append(tris.ravel() + off)
                off += len(p)
    return (np.concatenate(V).astype(np.float32), np.concatenate(N).astype(np.float32),
            np.concatenate(C).astype(np.float32), np.concatenate(I).astype(np.uint32))


# ----------------------------------------------------------------------------- ray caster

def trace(origin, dirs, boxes):
    Nr = len(dirs)
    tbest = np.full(Nr, np.inf, np.float32)
    bidx = np.full(Nr, -1, np.int32)
    nrm = np.zeros((Nr, 3), np.float32)
    for k, b in enumerate(boxes):
        c, s = math.cos(b["yaw"]), math.sin(b["yaw"])
        o = origin - b["c"]
        lo = np.array([c * o[0] + s * o[1], -s * o[0] + c * o[1], o[2]], np.float32)
        ld = np.stack([c * dirs[:, 0] + s * dirs[:, 1], -s * dirs[:, 0] + c * dirs[:, 1], dirs[:, 2]], 1)
        if np.all(np.abs(lo) < b["h"]):
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / np.where(np.abs(ld) < 1e-9, 1e-9, ld)
            t1 = (-b["h"][None] - lo[None]) * inv
            t2 = (b["h"][None] - lo[None]) * inv
        tmin = np.minimum(t1, t2)
        tmax = np.maximum(t1, t2)
        tnear = tmin.max(1)
        tfar = tmax.min(1)
        hit = (tnear <= tfar) & (tnear > 1e-3) & (tnear < tbest)
        if not hit.any():
            continue
        ax = tmin[hit].argmax(1)
        sgn = -np.sign(ld[hit, ax])
        nl = np.zeros((hit.sum(), 3), np.float32)
        nl[np.arange(len(ax)), ax] = sgn
        nw = np.stack([c * nl[:, 0] - s * nl[:, 1], s * nl[:, 0] + c * nl[:, 1], nl[:, 2]], 1)
        tbest[hit] = tnear[hit]
        bidx[hit] = k
        nrm[hit] = nw
    return tbest, bidx, nrm


def render_photo(cam, boxes, rooms):
    w, h = cam["w"], cam["h"]
    M = np.array(cam["c2w"]).reshape(4, 4)
    R, t = M[:3, :3], M[:3, 3]
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    d = np.stack([(u - cam["cx"]) / cam["fx"], (v - cam["cy"]) / cam["fy"], np.ones_like(u)], -1).reshape(-1, 3)
    d = (d @ R.T).astype(np.float32)
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    tb, bi, nr = trace(t.astype(np.float32), d, boxes)
    img = np.zeros((len(d), 3), np.float32)
    sky = d[:, 2:3]
    img[:] = np.array([0.80, 0.88, 0.97]) * (1 - 0.3 * np.clip(sky, 0, 1))
    for k in np.unique(bi):
        if k < 0:
            continue
        m = bi == k
        p = t[None] + d[m] * tb[m, None]
        img[m] = color_at(boxes[k], p, nr[m], rooms)
    # mild exposure falloff with distance, like a phone photo
    img = img.reshape(h, w, 3)
    yy, xx = np.mgrid[0:h, 0:w]
    vig = 1 - 0.18 * (((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / 2
    img = img * vig[..., None]
    im = Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))
    return im.filter(ImageFilter.GaussianBlur(0.6))


# ----------------------------------------------------------------------------- cameras + trajectory

PATH = [(0.6, 2.5), (0.7, 1.6), (-0.3, 1.3), (-1.5, 1.7), (-1.9, 2.05), (-1.2, 1.5), (-0.2, 1.2), (1.6, 1.3),
        (2.6, 1.3), (3.3, 1.5), (2.6, 1.15), (1.2, 1.0), (1.1, 0.0), (1.1, -0.4), (2.8, -0.6), (2.9, -1.9),
        (1.0, -2.5), (0.6, -1.4), (-1.0, -1.2), (-1.4, -2.2), (-3.1, -2.2), (-3.9, -0.9), (-3.0, -1.05)]


def resample(path, step):
    pts = np.array(path, float)
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    ss = np.arange(0, s[-1], step)
    return np.column_stack([np.interp(ss, s, pts[:, 0]), np.interp(ss, s, pts[:, 1])]), ss


def look_c2w(pos, yaw, pitch):
    f = np.array([math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)])
    x = np.cross(f, [0, 0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(f, x)
    M = np.eye(4)
    M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = x, y, f, pos
    return M


def make_cameras(bm, w, h, f):
    cams = []
    speed = 0.5
    pts, ss = resample(PATH, 0.8)
    for i, (p, s) in enumerate(zip(pts, ss)):
        j = min(i + 1, len(pts) - 1)
        tang = pts[j] - pts[max(0, j - 1)] if j > 0 else np.array([0, -1.0])
        yaw = math.atan2(tang[1], tang[0]) + math.radians(30 * math.sin(i * 1.3))
        z = 1.45 + 0.04 * math.sin(i * 0.7)
        M = look_c2w(np.array([p[0], p[1], z]), yaw, math.radians(-10))
        cams.append(dict(id=f"kf_{i:06d}", kind="keyframe", t=round(float(s / speed), 3), M=M))
    st = bm["storeys"][0]
    from plan_svg import polygon_centroid
    for r in st["rooms"]:
        cx, cy = polygon_centroid(r["polygon"])
        far = max(r["polygon"], key=lambda q: (q[0] - cx) ** 2 + (q[1] - cy) ** 2)
        yaw = math.atan2(far[1] - cy, far[0] - cx)
        # stand back from the far corner: opposite side of centroid
        pos = np.array([cx - 0.35 * (far[0] - cx), cy - 0.35 * (far[1] - cy), 1.5])
        tnear = float(ss[np.argmin(np.linalg.norm(pts - pos[:2], axis=1))] / speed)
        cams.append(dict(id=f"hero_{r['id']}", kind="hero", t=round(tnear + 0.01, 3),
                         M=look_c2w(pos, yaw, math.radians(-14)), room_id=r["id"]))
    cams.sort(key=lambda c: c["t"])
    rooms = st["rooms"]
    out = []
    for c in cams:
        ri = room_at(rooms, c["M"][None, :3, 3])[0]
        out.append({"id": c["id"], "kind": c["kind"], "t": c["t"],
                    "full": f"photos/full/{c['id']}.jpg", "thumb": f"photos/thumb/{c['id']}.jpg",
                    "w": w, "h": h, "fx": f, "fy": f, "cx": w / 2, "cy": h / 2,
                    "c2w": [round(float(x), 6) for x in c["M"].ravel()],
                    "room_id": rooms[ri]["id"] if ri >= 0 else None})
    traj_pts, traj_s = resample(PATH, speed / 10)
    traj = {"t": [round(float(s / speed), 2) for s in traj_s],
            "p": [[round(float(p[0]), 3), round(float(p[1]), 3), round(1.45 + 0.03 * math.sin(k * 0.4), 3)]
                  for k, p in enumerate(traj_pts)]}
    return out, traj


def meshopt_compress(paths):
    """Compress GLBs in place with gltfpack (EXT_meshopt_compression + quantization), as the pipeline does.
    Skipped with a warning if gltfpack (npm devDependency of viewer/) is unavailable."""
    import shutil
    import subprocess
    viewer_dir = Path(__file__).resolve().parent.parent
    exe = viewer_dir / "node_modules/.bin/gltfpack"
    cmd = [str(exe)] if exe.exists() else ([shutil.which("gltfpack")] if shutil.which("gltfpack") else None)
    if not cmd:
        print("warning: gltfpack not found (npm i in viewer/); GLBs left uncompressed")
        return
    for p in paths:
        tmp = p.with_suffix(".tmp.glb")
        subprocess.run(cmd + ["-i", str(p), "-o", str(tmp), "-cc", "-kn", "-km"], check=True, capture_output=True)
        tmp.replace(p)


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "public/fixtures/demo"))
    ap.add_argument("--splats", type=int, default=420_000)
    ap.add_argument("--fast", action="store_true", help="fewer splats, low-res photos")
    ap.add_argument("--no-meshopt", action="store_true", help="skip gltfpack compression")
    args = ap.parse_args()
    out = Path(args.out)
    for d in ("model", "mesh", "splat", "points", "plan", "photos/full", "photos/thumb"):
        (out / d).mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    bm = building_model()
    rooms = bm["storeys"][0]["rooms"]
    (out / "model/building_model.json").write_text(json.dumps(bm, indent=1))
    boxes = boxes_from_model(bm)
    print(f"{len(boxes)} boxes")

    # models
    write_glb(out / "model/architecture.glb", group_nodes(boxes, "architecture"))
    write_glb(out / "model/furniture.glb",
              group_nodes(boxes, "furniture", lambda b: {"kind": "furniture", "label": b.get("label")}))
    write_glb(out / "model/ceilings.glb", group_nodes(boxes, "ceilings"))
    V, N, C, I = tess_mesh(boxes, rooms)
    write_glb(out / "mesh/mesh.glb", [dict(name="mesh", V=V, N=N, I=I, colors=C)])
    n_tris = len(I) // 3
    if not args.no_meshopt:
        meshopt_compress([out / "model/architecture.glb", out / "model/furniture.glb", out / "model/ceilings.glb",
                          out / "mesh/mesh.glb"])
    print(f"mesh: {n_tris} tris")

    # splat
    target = 60_000 if args.fast else args.splats
    (P, Nn, Cc, S, Q), spacing = sample_scene(boxes, rooms, target)
    sp, sc, ss = sky_blobs(bm, 150 if args.fast else 700)
    xyz = np.concatenate([P, sp])
    nrm = np.concatenate([Nn, np.zeros_like(sp)])
    rgb = np.concatenate([Cc, sc])
    scl = np.concatenate([S, ss])
    quat = np.concatenate([Q, np.tile([1.0, 0, 0, 0], (len(sp), 1))])
    opa = np.concatenate([np.full(len(P), 0.92), np.full(len(sp), 0.3)])
    write_3dgs_ply(out / "splat/scene.ply", xyz, nrm, rgb, scl, quat, opa)
    n_splats = len(xyz)
    print(f"splat: {n_splats} gaussians, spacing {spacing * 100:.1f} cm")

    # points (subsample of the surface)
    k = min(len(P), 180_000)
    sel = RNG.choice(len(P), k, replace=False)
    jitter = (RNG.random((k, 3)) - 0.5) * 0.01
    write_points_ply(out / "points/points.ply", P[sel] + jitter, Cc[sel], Nn[sel])

    # plans
    for style in STYLES:
        (out / f"plan/plan_{style}.svg").write_text(render_plan_svg(bm, style))

    # cameras + photos
    W, H, F = (480, 360, 380.0) if args.fast else (960, 720, 760.0)
    cams, traj = make_cameras(bm, W, H, F)
    (out / "cameras.json").write_text(json.dumps({"cameras": cams}, indent=1))
    (out / "trajectory.json").write_text(json.dumps(traj))
    for i, c in enumerate(cams):
        im = render_photo(c, boxes, rooms)
        im.save(out / c["full"], quality=88)
        im.resize((256, round(256 * H / W)), Image.LANCZOS).save(out / c["thumb"], quality=80)
        print(f"\rphotos {i + 1}/{len(cams)}", end="", flush=True)
    print()

    n_kf = sum(c["kind"] == "keyframe" for c in cams)
    n_hero = len(cams) - n_kf
    ceil = [r["ceiling_z"] for r in rooms]
    scene = {
        "schema": "scene.v1", "scan_id": "scan_demo", "version": 1, "name": "Apartment",
        "captured_at": "2026-10-28T15:20:00Z", "created_at": "2026-10-28T16:02:00Z",
        "units": "m", "up": "z", "north_deg": bm["north_deg"],
        "bounds": {"min": [-4.7, -3.2, -0.05], "max": [4.7, 3.2, 2.7]},
        "levels": {"floor": 0.0, "ceiling": min(ceil), "ceiling_max": max(ceil)},
        "default_view": {"position": [5.2, -8.4, 8.6], "target": [0.0, 0.2, 0.6]},
        "layers": [
            {"id": "ceilings", "kind": "model", "label": "Ceilings", "url": "model/ceilings.glb", "visible": False,
             "summary": f"v01 · {min(ceil):.2f}–{max(ceil):.2f} m"},
            {"id": "splat", "kind": "splat", "label": "Splat", "url": "splat/scene.ply", "visible": True,
             "summary": f"iPhone · 3DGS · {n_splats // 1000} k",
             "stats": {"count": n_splats, "sh_degree": 0}},
            {"id": "architecture", "kind": "model", "label": "Model", "url": "model/architecture.glb", "visible": True,
             "summary": "v01 · walls · floors · doors · windows", "modes": ["solid", "edges"], "mode": "edges"},
            {"id": "furniture", "kind": "model", "label": "Model", "url": "model/furniture.glb", "visible": True,
             "summary": "v01 · furniture", "modes": ["solid", "edges"], "mode": "edges"},
            {"id": "mesh", "kind": "mesh", "label": "Mesh", "url": "mesh/mesh.glb", "visible": False,
             "summary": f"fixture · {n_tris // 1000} k tris", "modes": ["color", "normals", "wire"], "mode": "color"},
            {"id": "pointcloud", "kind": "points", "label": "Point cloud", "url": "points/points.ply", "visible": False,
             "summary": f"{k // 1000} k · back faces hidden"},
            {"id": "photos", "kind": "cameras", "label": "Photos", "url": "cameras.json", "visible": True,
             "summary": f"{len(cams)} photos · {n_hero} hero", "modes": ["keyframes", "hero", "all"], "mode": "all"},
            {"id": "trajectory", "kind": "trajectory", "label": "Trajectory", "url": "trajectory.json", "visible": False,
             "summary": f"{traj['t'][-1]:.0f} s walk"},
            {"id": "plan", "kind": "plan", "label": "Plan", "visible": True, "summary": "Stylized plan",
             "styles": [{"id": s, "url": f"plan/plan_{s}.svg"} for s in STYLES], "style": "stylized"},
            {"id": "ground", "kind": "ground", "label": "Ground", "visible": True, "cell": 0.5, "fade": 4.0,
             "modes": ["lines", "checker"], "mode": "lines"},
        ],
        "building_model_url": "model/building_model.json",
        "stats": {"processing_s": 0, "frames": len(traj["t"]), "registered": len(cams)},
    }
    (out / "scene.json").write_text(json.dumps(scene, indent=1))
    print(f"wrote {out} in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
