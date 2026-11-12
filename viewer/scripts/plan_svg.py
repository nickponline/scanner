"""Stylised floor-plan SVG renderer for Scanner building models.

Pure python (stdlib only) so the pipeline can import it unchanged:

    from plan_svg import render_plan_svg, STYLES
    svg = render_plan_svg(building_model, "stylized")

Output follows CONTRACTS.md §4 "Plan SVG mapping":
  * root <svg> carries data-world-bounds="minx miny maxx maxy" and data-px-per-m
  * world (x, y) -> svg (u, v) = ((x - minx) * s, (maxy - y) * s)
  * rooms are <g class="room" data-room-id="…">
  * camera positions are NOT baked in unless `cameras` is passed explicitly (debug use).
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

STYLES = ("stylized", "technical", "mono")
PX_PER_M = 100.0
MARGIN_M = 0.9
FONT = "Inter, 'Helvetica Neue', Helvetica, Arial, sans-serif"

# Soft pastel fills per room type (stylized); matches the reference minimap.
PASTELS = {
    "living": "#dcebd9",
    "living_room": "#dcebd9",
    "kitchen": "#fbf0cc",
    "dining": "#f6e0d6",
    "dining_room": "#f6e0d6",
    "bedroom": "#e4def2",
    "bathroom": "#d8e9f3",
    "bath": "#d8e9f3",
    "wc": "#d8e9f3",
    "hall": "#ececec",
    "hallway": "#ececec",
    "entrance": "#ececec",
    "corridor": "#ececec",
    "closet": "#f1ede6",
    "storage": "#f1ede6",
    "office": "#e0eef0",
    "study": "#e0eef0",
    "laundry": "#e6eef6",
}
FALLBACK_PASTELS = ["#e8e2f0", "#dcebd9", "#fbf0cc", "#f6e0d6", "#d8e9f3", "#efe9df"]
MONO_FILLS = ["#f4f4f4", "#ececec", "#e4e4e4", "#f0f0f0", "#e8e8e8"]


# --------------------------------------------------------------------------- geometry helpers

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _mul(a, k):
    return (a[0] * k, a[1] * k)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _norm(a):
    length = math.hypot(a[0], a[1]) or 1.0
    return (a[0] / length, a[1] / length)


def polygon_area(poly: Sequence[Sequence[float]]) -> float:
    a = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def polygon_centroid(poly: Sequence[Sequence[float]]):
    a = cx = cy = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        c = x0 * y1 - x1 * y0
        a += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    if abs(a) < 1e-9:
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    return (cx / (3 * a), cy / (3 * a))


def _poly_bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def wall_pieces(wall: dict, openings: Iterable[dict], extend_ends: bool = True):
    """Split a wall centreline into solid intervals [(s0, s1), …] along its length, skipping openings.

    Returns (origin, dir, normal, length, intervals, opening_intervals).
    Intervals are in metres along `dir` from `origin` (= wall start). Ends are extended by half the
    thickness so perpendicular walls join without notches.
    """
    p0 = tuple(wall["start"])
    p1 = tuple(wall["end"])
    d = _norm(_sub(p1, p0))
    n = (-d[1], d[0])
    length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    t = wall.get("thickness", 0.12)
    ext = t / 2 if extend_ends else 0.0
    holes = []
    for o in openings:
        if o.get("wall_id") != wall["id"]:
            continue
        s = _dot(_sub(tuple(o["center"]), p0), d)
        w = o.get("width", 0.8)
        holes.append((max(-ext, s - w / 2), min(length + ext, s + w / 2), o))
    holes.sort(key=lambda h: h[0])
    solid = []
    cur = -ext
    for a, b, _ in holes:
        if a > cur + 1e-4:
            solid.append((cur, a))
        cur = max(cur, b)
    if length + ext > cur + 1e-4:
        solid.append((cur, length + ext))
    return p0, d, n, length, solid, holes


# --------------------------------------------------------------------------- svg writer

class _Svg:
    def __init__(self, minx, miny, maxx, maxy, s):
        self.minx, self.miny, self.maxx, self.maxy, self.s = minx, miny, maxx, maxy, s
        self.parts: list[str] = []

    def uv(self, p):
        return ((p[0] - self.minx) * self.s, (self.maxy - p[1]) * self.s)

    def pts(self, poly):
        return " ".join(f"{u:.1f},{v:.1f}" for u, v in (self.uv(p) for p in poly))

    def add(self, s: str):
        self.parts.append(s)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _room_fill(room: dict, idx: int, style: str) -> str:
    if style == "technical":
        return "#ffffff"
    if style == "mono":
        return MONO_FILLS[idx % len(MONO_FILLS)]
    t = (room.get("type") or "").lower()
    return PASTELS.get(t) or FALLBACK_PASTELS[idx % len(FALLBACK_PASTELS)]


def _nice_scale_len(width_m: float) -> float:
    target = width_m / 6.0
    for v in (0.5, 1, 2, 5, 10, 20, 50):
        if v >= target:
            return float(v)
    return 100.0


def render_plan_svg(building_model: dict, style: str = "stylized", cameras=None,
                    px_per_m: float = PX_PER_M, storey_index: int = 0) -> str:
    """Render one storey of a building_model.v1 dict as an SVG string.

    style: one of STYLES ("stylized" pastel, "technical" hatched + dimensions, "mono" greyscale).
    cameras: optional iterable of {"c2w": [16], …} or [x, y] points to overlay as red dots (debug only;
             the contract says production plans do not bake cameras in).
    """
    if style not in STYLES:
        raise ValueError(f"unknown plan style {style!r}; expected one of {STYLES}")
    storey = building_model["storeys"][storey_index]
    rooms = storey.get("rooms", [])
    walls = storey.get("walls", [])
    openings = storey.get("openings", [])
    furniture = storey.get("furniture", [])
    nd = building_model.get("north_deg")
    north_deg = float(nd) if nd is not None else None  # null = no compass → no north arrow

    # ---- bounds (walls + rooms) + margin
    xs, ys = [], []
    for w in walls:
        t = w.get("thickness", 0.12)
        for p in (w["start"], w["end"]):
            xs += [p[0] - t, p[0] + t]
            ys += [p[1] - t, p[1] + t]
    for r in rooms:
        for p in r["polygon"]:
            xs.append(p[0])
            ys.append(p[1])
    if not xs:
        xs, ys = [-1, 1], [-1, 1]
    minx, miny = min(xs) - MARGIN_M, min(ys) - MARGIN_M
    maxx, maxy = max(xs) + MARGIN_M, max(ys) + MARGIN_M
    s = px_per_m
    W, H = (maxx - minx) * s, (maxy - miny) * s
    svg = _Svg(minx, miny, maxx, maxy, s)

    ink = "#1b1b1b" if style != "mono" else "#3a3a3a"
    label_color = "#1f1f1f" if style != "mono" else "#333333"
    sub_color = "#6b6b6b"
    fs_name = 0.30 * s
    fs_area = 0.20 * s

    svg.add(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
        f'viewBox="0 0 {W:.1f} {H:.1f}" data-world-bounds="{minx:.3f} {miny:.3f} {maxx:.3f} {maxy:.3f}" '
        f'data-px-per-m="{s:g}" data-style="{style}"'
        + (f' data-north-deg="{north_deg:g}"' if north_deg is not None else "") + '>'
    )
    svg.add("<defs>"
            '<pattern id="hatch" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">'
            f'<rect width="8" height="8" fill="#ffffff"/><line x1="0" y1="0" x2="0" y2="8" stroke="{ink}" stroke-width="2.2"/>'
            "</pattern>"
            '<marker id="dimtick" viewBox="-5 -5 10 10" markerWidth="10" markerHeight="10" orient="auto">'
            f'<line x1="-4" y1="4" x2="4" y2="-4" stroke="{ink}" stroke-width="1.5"/></marker>'
            "</defs>")
    svg.add(f'<rect class="background" x="0" y="0" width="{W:.1f}" height="{H:.1f}" fill="#ffffff"/>')

    # ---- rooms
    svg.add('<g class="rooms">')
    for i, r in enumerate(rooms):
        fill = _room_fill(r, i, style)
        svg.add(f'<g class="room" data-room-id="{_esc(str(r["id"]))}" data-room-type="{_esc(str(r.get("type", "")))}">'
                f'<polygon points="{svg.pts(r["polygon"])}" fill="{fill}" stroke="none"/></g>')
    svg.add("</g>")

    # ---- furniture (light outlines)
    if style != "mono" and furniture:
        stroke = "#a7a7a7" if style == "stylized" else "#8a8a8a"
        svg.add(f'<g class="furniture" fill="none" stroke="{stroke}" stroke-width="2">')
        for f in furniture:
            cx, cy = f["center"][0], f["center"][1]
            sx, sy = f["size"][0], f["size"][1]
            yaw = math.radians(f.get("yaw_deg", 0.0))
            c, sn = math.cos(yaw), math.sin(yaw)
            corners = []
            for dx, dy in ((-sx / 2, -sy / 2), (sx / 2, -sy / 2), (sx / 2, sy / 2), (-sx / 2, sy / 2)):
                corners.append((cx + c * dx - sn * dy, cy + sn * dx + c * dy))
            label = (f.get("label") or "").lower()
            if label in ("table", "dining_table", "round_table") and abs(sx - sy) < 0.05:
                u, v = svg.uv((cx, cy))
                svg.add(f'<circle cx="{u:.1f}" cy="{v:.1f}" r="{sx / 2 * s:.1f}" data-furniture-id="{_esc(str(f["id"]))}"/>')
            else:
                svg.add(f'<polygon points="{svg.pts(corners)}" data-furniture-id="{_esc(str(f["id"]))}"/>')
        svg.add("</g>")

    # ---- walls (poché) + openings
    wall_fill = {"stylized": ink, "technical": "url(#hatch)", "mono": ink}[style]
    wall_stroke = ' stroke="#1b1b1b" stroke-width="2.5"' if style == "technical" else ""
    svg.add(f'<g class="walls" fill="{wall_fill}"{wall_stroke}>')
    by_wall = {w["id"]: w for w in walls}
    for w in walls:
        p0, d, n, length, solid, _ = wall_pieces(w, openings)
        t = w.get("thickness", 0.12)
        for a, b in solid:
            q0 = _add(p0, _mul(d, a))
            q1 = _add(p0, _mul(d, b))
            poly = [_add(q0, _mul(n, t / 2)), _add(q1, _mul(n, t / 2)),
                    _add(q1, _mul(n, -t / 2)), _add(q0, _mul(n, -t / 2))]
            svg.add(f'<polygon data-wall-id="{_esc(str(w["id"]))}" points="{svg.pts(poly)}"/>')
    svg.add("</g>")

    svg.add(f'<g class="openings" fill="none" stroke="{ink}" stroke-width="2">')
    for o in openings:
        w = by_wall.get(o.get("wall_id"))
        if w is None:
            continue
        p0 = tuple(w["start"])
        d = _norm(_sub(tuple(w["end"]), p0))
        n = (-d[1], d[0])
        t = w.get("thickness", 0.12)
        c = tuple(o["center"])
        width = o.get("width", 0.8)
        a = _add(c, _mul(d, -width / 2))
        b = _add(c, _mul(d, width / 2))
        kind = o.get("type", "door")
        if kind == "window":
            poly = [_add(a, _mul(n, t / 2)), _add(b, _mul(n, t / 2)), _add(b, _mul(n, -t / 2)), _add(a, _mul(n, -t / 2))]
            svg.add(f'<polygon class="window" data-opening-id="{_esc(str(o["id"]))}" points="{svg.pts(poly)}" fill="#ffffff" stroke-width="1.6"/>')
            if style != "mono":
                svg.add(f'<polyline points="{svg.pts([a, b])}" stroke-width="1.4"/>')
                if style == "technical":
                    for k in (-t / 6, t / 6):
                        svg.add(f'<polyline points="{svg.pts([_add(a, _mul(n, k)), _add(b, _mul(n, k))])}" stroke-width="0.9"/>')
        elif kind == "door":
            swing = o.get("swing") or {"hinge": "start", "side": "left"}
            hinge, other = (a, b) if swing.get("hinge", "start") == "start" else (b, a)
            sgn = 1.0 if swing.get("side", "left") == "left" else -1.0
            tip = _add(hinge, _mul(n, sgn * width))
            hu, hv = svg.uv(hinge)
            tu, tv = svg.uv(tip)
            ou, ov = svg.uv(other)
            cross = (tu - hu) * (ov - hv) - (tv - hv) * (ou - hu)
            sweep = 1 if cross > 0 else 0
            r = width * s
            svg.add(f'<g class="door" data-opening-id="{_esc(str(o["id"]))}">'
                    f'<line x1="{hu:.1f}" y1="{hv:.1f}" x2="{tu:.1f}" y2="{tv:.1f}" stroke-width="2.4"/>'
                    f'<path d="M {tu:.1f} {tv:.1f} A {r:.1f} {r:.1f} 0 0 {sweep} {ou:.1f} {ov:.1f}" '
                    f'stroke-width="1.1" stroke-dasharray="{"none" if style != "technical" else "6 4"}"/></g>')
    svg.add("</g>")

    # ---- labels
    svg.add(f'<g class="labels" font-family="{FONT}" text-anchor="middle">')
    for r in rooms:
        cx, cy = polygon_centroid(r["polygon"])
        u, v = svg.uv((cx, cy))
        bx0, by0, bx1, by1 = _poly_bbox(r["polygon"])
        name = str(r.get("name", r.get("type", ""))).upper()
        area = r.get("area_m2")
        if area is None:
            area = polygon_area(r["polygon"])
        room_w, room_h = (bx1 - bx0) * s, (by1 - by0) * s
        fs = fs_name
        est_w = 0.62 * fs * len(name)
        rotate = ""
        if est_w > room_w * 0.92 and room_h > room_w * 1.4:
            rotate = f' transform="rotate(-90 {u:.1f} {v:.1f})"'
            fs = min(fs, room_h * 0.92 / max(1, 0.62 * len(name)))
        elif est_w > room_w * 0.92:
            fs = room_w * 0.92 / max(1, 0.62 * len(name))
        weight = "600" if style != "technical" else "500"
        svg.add(f'<g class="room-label" data-room-id="{_esc(str(r["id"]))}"{rotate}>'
                f'<text x="{u:.1f}" y="{v:.1f}" font-size="{fs:.1f}" font-weight="{weight}" fill="{label_color}" '
                f'letter-spacing="{fs * 0.02:.2f}">{_esc(name)}</text>'
                f'<text x="{u:.1f}" y="{v + fs_area * 1.25:.1f}" font-size="{fs_area:.1f}" fill="{sub_color}">'
                f'{area:.1f} m²</text></g>')
    svg.add("</g>")

    # ---- technical dimensions
    if style == "technical" and walls:
        ext_xs = [p for w in walls for p in (w["start"][0], w["end"][0])]
        ext_ys = [p for w in walls for p in (w["start"][1], w["end"][1])]
        x0, x1, y0, y1 = min(ext_xs), max(ext_xs), min(ext_ys), max(ext_ys)
        off = 0.42
        svg.add(f'<g class="dimensions" stroke="{ink}" stroke-width="1.2" font-family="{FONT}" font-size="{0.15 * s:.1f}" fill="{ink}">')
        (u0, v0), (u1, _) = svg.uv((x0, y1 + off)), svg.uv((x1, y1 + off))
        svg.add(f'<line x1="{u0:.1f}" y1="{v0:.1f}" x2="{u1:.1f}" y2="{v0:.1f}" marker-start="url(#dimtick)" marker-end="url(#dimtick)"/>')
        svg.add(f'<text x="{(u0 + u1) / 2:.1f}" y="{v0 - 6:.1f}" text-anchor="middle" stroke="none">{x1 - x0:.2f} m</text>')
        (u0, v0), (_, v1) = svg.uv((x1 + off, y1)), svg.uv((x1 + off, y0))
        svg.add(f'<line x1="{u0:.1f}" y1="{v0:.1f}" x2="{u0:.1f}" y2="{v1:.1f}" marker-start="url(#dimtick)" marker-end="url(#dimtick)"/>')
        svg.add(f'<text x="{u0 + 6:.1f}" y="{(v0 + v1) / 2:.1f}" text-anchor="middle" stroke="none" '
                f'transform="rotate(90 {u0 + 6:.1f} {(v0 + v1) / 2:.1f})" dy="-4">{y1 - y0:.2f} m</text>')
        svg.add("</g>")

    # ---- optional debug cameras
    if cameras:
        svg.add('<g class="cameras" fill="#e8442e" stroke="#ffffff" stroke-width="2">')
        for cam in cameras:
            if isinstance(cam, dict) and "c2w" in cam:
                m = cam["c2w"]
                x, y = m[3], m[7]
            else:
                x, y = cam[0], cam[1]
            u, v = svg.uv((x, y))
            svg.add(f'<circle cx="{u:.1f}" cy="{v:.1f}" r="9"/>')
        svg.add("</g>")

    # ---- annotations: north arrow + scale bar
    svg.add(f'<g class="annotations" font-family="{FONT}" fill="{ink}" stroke="{ink}">')
    na_u, na_v = W - 0.42 * s, 0.42 * s
    if north_deg is not None:
        svg.add(f'<g class="north-arrow" transform="translate({na_u:.1f} {na_v:.1f}) rotate({-north_deg:.2f})">'
            f'<polygon points="0,{-0.36 * s:.1f} {0.14 * s:.1f},{0.08 * s:.1f} 0,{-0.01 * s:.1f} {-0.14 * s:.1f},{0.08 * s:.1f}" stroke="none"/>'
            f'<text x="0" y="{0.36 * s:.1f}" font-size="{0.28 * s:.1f}" font-weight="700" text-anchor="middle" stroke="none">N</text></g>')
    bar = _nice_scale_len(maxx - minx - 2 * MARGIN_M)
    bu, bv = 0.3 * s, H - 0.28 * s
    svg.add(f'<g class="scale-bar" stroke-width="2.2">'
            f'<line x1="{bu:.1f}" y1="{bv:.1f}" x2="{bu + bar * s:.1f}" y2="{bv:.1f}"/>'
            f'<line x1="{bu:.1f}" y1="{bv - 7:.1f}" x2="{bu:.1f}" y2="{bv + 7:.1f}"/>'
            f'<line x1="{bu + bar * s:.1f}" y1="{bv - 7:.1f}" x2="{bu + bar * s:.1f}" y2="{bv + 7:.1f}"/>'
            f'<text x="{bu + bar * s / 2:.1f}" y="{bv - 0.08 * s:.1f}" font-size="{0.22 * s:.1f}" text-anchor="middle" '
            f'stroke="none">{bar:g} m</text></g>')
    svg.add("</g>")
    svg.add("</svg>")
    return "\n".join(svg.parts)


if __name__ == "__main__":  # pragma: no cover - manual use
    import json
    import sys

    if len(sys.argv) < 3:
        print("usage: plan_svg.py building_model.json style [out.svg]", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1]) as fh:
        bm = json.load(fh)
    out = render_plan_svg(bm, sys.argv[2])
    if len(sys.argv) > 3:
        with open(sys.argv[3], "w") as fh:
            fh.write(out)
    else:
        print(out)
