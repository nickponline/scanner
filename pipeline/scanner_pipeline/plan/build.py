"""Validate the agent's proposal, turn it into building_model.v1, and build GLBs / overlays."""
from __future__ import annotations

import math

import cv2
import numpy as np

from .geometry import Raster, Scene, render_topdown, wall_evidence, wall_support

ROOM_TYPES = ["living", "kitchen", "dining", "bedroom", "bathroom", "wc", "hall", "corridor", "closet", "office",
              "utility", "laundry", "stairs", "garage", "balcony", "other"]


def _poly(p):
    from shapely.geometry import Polygon

    return Polygon(p)


def validate(model: dict, sc: Scene, ev=None) -> tuple[list[str], list[str], dict]:
    """Returns (errors, warnings, derived) — errors block submission."""
    from shapely.geometry import LineString, Point

    errs, warns = [], []
    derived = {"rooms": {}, "walls": {}}
    walls = {w.get("id"): w for w in model.get("walls", [])}
    if not model.get("rooms"):
        errs.append("no rooms")
    if not walls:
        errs.append("no walls")
    ev = ev or wall_evidence(sc)
    for wid, w in walls.items():
        try:
            s, e = np.asarray(w["start"], float), np.asarray(w["end"], float)
        except Exception:  # noqa: BLE001
            errs.append(f"wall {wid}: bad start/end")
            continue
        L = float(np.linalg.norm(e - s))
        if L < 0.1:
            errs.append(f"wall {wid}: shorter than 10 cm")
        th = float(w.get("thickness", 0.12))
        if not 0.04 <= th <= 0.6:
            errs.append(f"wall {wid}: thickness {th} outside 0.04–0.6 m")
        sup = wall_support(sc, s, e, ev)
        derived["walls"][wid] = {"length": round(L, 3), "evidence_support": round(sup, 2)}
        if sup < 0.25:
            warns.append(f"wall {wid}: only {sup:.0%} of its length has scan evidence — check it exists/positions")
    polys = {}
    for r in model.get("rooms", []):
        rid = r.get("id")
        try:
            P = _poly(r["polygon"])
        except Exception:  # noqa: BLE001
            errs.append(f"room {rid}: bad polygon")
            continue
        if not P.is_valid:
            errs.append(f"room {rid}: self-intersecting polygon")
            continue
        if P.area < 0.5:
            errs.append(f"room {rid}: area {P.area:.2f} m² too small")
        if r.get("type") not in ROOM_TYPES:
            warns.append(f"room {rid}: type '{r.get('type')}' not in {ROOM_TYPES}")
        polys[rid] = P
        derived["rooms"][rid] = {"area_m2": round(P.area, 2)}
    ids = list(polys)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            inter = polys[ids[i]].intersection(polys[ids[j]]).area
            if inter > 0.05 * min(polys[ids[i]].area, polys[ids[j]].area) and inter > 0.1:
                errs.append(f"rooms {ids[i]} and {ids[j]} overlap by {inter:.2f} m²")
    for o in model.get("openings", []):
        oid, w = o.get("id"), walls.get(o.get("wall_id"))
        if w is None:
            errs.append(f"opening {oid}: wall_id {o.get('wall_id')} not found")
            continue
        line = LineString([w["start"], w["end"]])
        c = Point(o["center"])
        if line.distance(c) > 0.12:
            errs.append(f"opening {oid}: centre {line.distance(c):.2f} m off its wall")
        t = line.project(c)
        wd = float(o.get("width", 0.8))
        if t - wd / 2 < -0.05 or t + wd / 2 > line.length + 0.05:
            errs.append(f"opening {oid}: extends beyond the ends of wall {o['wall_id']}")
        if o.get("type") not in ("door", "window", "opening"):
            errs.append(f"opening {oid}: type must be door|window|opening")
        if not 0.3 <= wd <= 4.0:
            errs.append(f"opening {oid}: width {wd} outside 0.3–4 m")
    for f in model.get("furniture", []):
        c = Point(f["center"][:2])
        if polys and not any(P.buffer(0.2).contains(c) for P in polys.values()):
            warns.append(f"furniture {f.get('id')} ({f.get('label')}) is outside every room")
    derived["total_area_m2"] = round(sum(P.area for P in polys.values()), 2)
    return errs, warns, derived


def overlay(model: dict, sc: Scene, show_furniture=True):
    img, r = render_topdown(sc, mode="color")
    img = (img.astype(np.float32) * 0.55 + 255 * 0.45).astype(np.uint8)
    pal = [(180, 220, 255), (200, 255, 200), (255, 220, 200), (230, 200, 255), (200, 240, 240), (255, 240, 180)]
    layer = img.copy()
    for k, rm in enumerate(model.get("rooms", [])):
        try:
            pts = r.px(np.asarray(rm["polygon"], float)).astype(np.int32)
        except Exception:  # noqa: BLE001
            continue
        cv2.fillPoly(layer, [pts], pal[k % len(pal)])
    img = cv2.addWeighted(layer, 0.5, img, 0.5, 0)
    for k, rm in enumerate(model.get("rooms", [])):
        try:
            c = np.asarray(rm["polygon"], float).mean(0)
            u, v = r.px(c)
            cv2.putText(img, f"{rm.get('id')}:{rm.get('name', '')}", (int(u) - 40, int(v)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
        except Exception:  # noqa: BLE001
            pass
    for w in model.get("walls", []):
        try:
            a, b = r.px(w["start"]).astype(int), r.px(w["end"]).astype(int)
        except Exception:  # noqa: BLE001
            continue
        th = max(2, int(float(w.get("thickness", 0.12)) * r.s))
        cv2.line(img, tuple(a), tuple(b), (0, 0, 200), th)
        m = (a + b) // 2
        cv2.putText(img, str(w.get("id")), (int(m[0]) + 3, int(m[1]) - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 150), 1, cv2.LINE_AA)
    wmap = {w.get("id"): w for w in model.get("walls", [])}
    for o in model.get("openings", []):
        w = wmap.get(o.get("wall_id"))
        if not w:
            continue
        s, e = np.asarray(w["start"], float), np.asarray(w["end"], float)
        d = (e - s) / max(np.linalg.norm(e - s), 1e-6)
        c = np.asarray(o["center"], float)
        a, b = r.px(c - d * o.get("width", 0.8) / 2).astype(int), r.px(c + d * o.get("width", 0.8) / 2).astype(int)
        col = {"door": (220, 120, 0), "window": (230, 200, 0), "opening": (0, 170, 0)}.get(o.get("type"), (0, 0, 0))
        cv2.line(img, tuple(a), tuple(b), col, max(3, int(0.08 * r.s)))
        cv2.putText(img, str(o.get("id")), tuple(r.px(c).astype(int) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1, cv2.LINE_AA)
    if show_furniture:
        for f in model.get("furniture", []):
            try:
                (cx, cy), (sx, sy), yaw = f["center"][:2], f["size"][:2], f.get("yaw_deg", 0)
                box = cv2.boxPoints(((cx, cy), (sx, sy), yaw))
                cv2.polylines(img, [r.px(box).astype(np.int32)], True, (0, 140, 0), 2)
                u, v = r.px([cx, cy])
                cv2.putText(img, f"{f.get('id')}:{f.get('label')}", (int(u) - 20, int(v)), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 100, 0), 1, cv2.LINE_AA)
            except Exception:  # noqa: BLE001
                pass
    return img


def to_building_model(model: dict, sc: Scene, derived: dict, north_deg, provenance: dict) -> dict:
    ceiling = round(float(sc.ceiling), 3)
    rooms = []
    for r in model["rooms"]:
        rooms.append({"id": r["id"], "name": r.get("name") or r.get("type", "room").title(), "type": r.get("type", "other"),
                      "polygon": [[round(float(x), 3), round(float(y), 3)] for x, y in r["polygon"]],
                      "area_m2": derived["rooms"].get(r["id"], {}).get("area_m2"),
                      "ceiling_z": round(float(r.get("ceiling_z", ceiling)), 3)})
    walls = [{"id": w["id"], "start": [round(float(v), 3) for v in w["start"]], "end": [round(float(v), 3) for v in w["end"]],
              "thickness": round(float(w.get("thickness", 0.12)), 3), "height": round(float(w.get("height", ceiling)), 3),
              "base_z": 0.0, "exterior": bool(w.get("exterior", False))} for w in model["walls"]]
    openings = []
    for o in model.get("openings", []):
        t = o["type"]
        oo = {"id": o["id"], "type": t, "wall_id": o["wall_id"], "center": [round(float(v), 3) for v in o["center"]],
              "width": round(float(o.get("width", 0.8)), 3),
              "height": round(float(o.get("height", 2.03 if t != "window" else 1.2)), 3),
              "sill_z": round(float(o.get("sill_z", 0.0 if t != "window" else 0.9)), 3)}
        if t == "door" and o.get("swing"):
            oo["swing"] = o["swing"]
        openings.append(oo)
    furn = [{"id": f["id"], "label": f.get("label", "object"), "center": [round(float(v), 3) for v in f["center"]],
             "size": [round(float(v), 3) for v in f["size"]], "yaw_deg": round(float(f.get("yaw_deg", 0)), 1),
             "room_id": f.get("room_id")} for f in model.get("furniture", [])]
    return {"schema": "building_model.v1", "units": "m", "up": "z", "north_deg": north_deg,
            "storeys": [{"id": "s0", "name": "Ground", "floor_z": 0.0, "ceiling_z": ceiling, "rooms": rooms,
                         "walls": walls, "openings": openings, "furniture": furn}],
            "provenance": provenance}


# --------------------------------------------------------------------------- GLB export
def _box(center, size, yaw_rad):
    import trimesh

    m = trimesh.creation.box(extents=size)
    T = np.eye(4)
    T[:3, :3] = trimesh.transformations.rotation_matrix(yaw_rad, [0, 0, 1])[:3, :3]
    T[:3, 3] = center
    m.apply_transform(T)
    return m


def _color(m, rgba):
    m.visual.vertex_colors = np.tile(np.asarray(rgba, np.uint8), (len(m.vertices), 1))
    return m


def build_glbs(bm: dict) -> dict:
    """Return {'architecture': trimesh.Scene, 'furniture': ..., 'ceilings': ...} in World Z-up coords."""
    import trimesh
    from shapely.geometry import Polygon

    st = bm["storeys"][0]
    arch, furn, ceil = trimesh.Scene(), trimesh.Scene(), trimesh.Scene()
    ops_by_wall: dict[str, list] = {}
    for o in st["openings"]:
        ops_by_wall.setdefault(o["wall_id"], []).append(o)
    for w in st["walls"]:
        s, e = np.asarray(w["start"], float), np.asarray(w["end"], float)
        L = float(np.linalg.norm(e - s))
        if L < 1e-3:
            continue
        d = (e - s) / L
        yaw = math.atan2(d[1], d[0])
        H, th = w["height"], w["thickness"]
        cuts = []
        for o in ops_by_wall.get(w["id"], []):
            t = float((np.asarray(o["center"]) - s) @ d)
            cuts.append((max(0, t - o["width"] / 2), min(L, t + o["width"] / 2), o["sill_z"], o["sill_z"] + o["height"]))
        cuts.sort()
        pieces = []
        t0 = 0.0
        for a, b, z0, z1 in cuts:
            if a > t0:
                pieces.append((t0, a, 0, H))
            if z0 > 0.01:
                pieces.append((a, b, 0, z0))
            if z1 < H - 0.01:
                pieces.append((a, b, z1, H))
            t0 = max(t0, b)
        if t0 < L:
            pieces.append((t0, L, 0, H))
        parts = []
        for a, b, z0, z1 in pieces:
            if b - a < 1e-3 or z1 - z0 < 1e-3:
                continue
            c = s + d * (a + b) / 2
            parts.append(_box([c[0], c[1], (z0 + z1) / 2], [b - a, th, z1 - z0], yaw))
        if parts:
            arch.add_geometry(_color(trimesh.util.concatenate(parts), [235, 235, 235, 255]), node_name=w["id"], geom_name=w["id"])
        for o in ops_by_wall.get(w["id"], []):
            c = np.asarray(o["center"], float)
            if o["type"] == "door":
                m = _box([c[0], c[1], o["sill_z"] + o["height"] / 2], [o["width"] - 0.02, 0.04, o["height"]], yaw)
                arch.add_geometry(_color(m, [170, 120, 80, 255]), node_name=o["id"], geom_name=o["id"])
            elif o["type"] == "window":
                m = _box([c[0], c[1], o["sill_z"] + o["height"] / 2], [o["width"], 0.02, o["height"]], yaw)
                arch.add_geometry(_color(m, [150, 200, 240, 160]), node_name=o["id"], geom_name=o["id"])
    for r in st["rooms"]:
        P = Polygon(r["polygon"])
        if not P.is_valid or P.area < 0.1:
            continue
        fl = trimesh.creation.extrude_polygon(P, 0.02)
        fl.apply_translation([0, 0, -0.02])
        arch.add_geometry(_color(fl, [210, 200, 185, 255]), node_name=f"floor_{r['id']}", geom_name=f"floor_{r['id']}")
        cz = r.get("ceiling_z", st["ceiling_z"])
        cl = trimesh.creation.extrude_polygon(P, 0.02)
        cl.apply_translation([0, 0, cz])
        ceil.add_geometry(_color(cl, [245, 245, 245, 255]), node_name=f"ceiling_{r['id']}", geom_name=f"ceiling_{r['id']}")
    for f in st["furniture"]:
        m = _box(f["center"], f["size"], math.radians(f.get("yaw_deg", 0)))
        furn.add_geometry(_color(m, [200, 180, 150, 255]), node_name=f["id"], geom_name=f["id"])
    return {"architecture": arch, "furniture": furn, "ceilings": ceil}


def raster_for(sc: Scene) -> Raster:
    return Raster(sc.lo, sc.hi)
