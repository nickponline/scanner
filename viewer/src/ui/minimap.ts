import type { Viewer } from "../viewer";
import { camCenterWorld } from "../frames";
import type { PlanInfo } from "../layers/plan";
import { h, s } from "./dom";

const MAX_W = 220;
const MAX_H = 270;

/** Bottom-left white plan card: camera dots, "you are here" + view cone, click to fly, drag cone to turn. */
export class Minimap {
  readonly el: HTMLElement;
  private img: HTMLImageElement;
  private svg: SVGSVGElement;
  private dyn: SVGGElement;
  private info: PlanInfo | null = null;
  private bounds: [number, number, number, number];
  private pxPerM = 100;
  private W = 1;
  private H = 1;
  private dragging = false;
  private lastKey = "";

  constructor(private v: Viewer) {
    const b = v.scene.bounds;
    this.bounds = [b.min[0], b.min[1], b.max[0], b.max[1]];
    this.img = h("img", { class: "plan-img", alt: "floor plan", draggable: "false" });
    this.svg = s("svg", { class: "plan-overlay" });
    this.dyn = s("g");
    this.svg.append(this.dyn);
    this.el = h("div", { class: "minimap", "data-testid": "minimap" }, this.img, this.svg);
    this.setGeometry();
    this.bindPointer();
    v.addEventListener("change", () => this.refresh());
    this.refresh();
  }

  private setGeometry() {
    const [x0, y0, x1, y1] = this.bounds;
    if (!this.info) {
      this.pxPerM = 100;
      this.W = (x1 - x0) * this.pxPerM;
      this.H = (y1 - y0) * this.pxPerM;
    }
    const k = Math.min(MAX_W / this.W, MAX_H / this.H);
    this.el.style.width = `${Math.round(this.W * k)}px`;
    this.el.style.height = `${Math.round(this.H * k)}px`;
    this.svg.setAttribute("viewBox", `0 0 ${this.W} ${this.H}`);
  }

  /** CSS px → SVG units */
  private get k() {
    return this.el.clientWidth ? this.W / this.el.clientWidth : 1;
  }

  private uv(x: number, y: number): [number, number] {
    return [(x - this.bounds[0]) * this.pxPerM, (this.bounds[3] - y) * this.pxPerM];
  }

  private xy(u: number, v: number): [number, number] {
    return [u / this.pxPerM + this.bounds[0], this.bounds[3] - v / this.pxPerM];
  }

  private refresh() {
    const plan = this.v.plan;
    const info = plan?.info ?? null;
    if (info && info !== this.info) {
      this.info = info;
      this.bounds = info.bounds;
      this.pxPerM = info.pxPerM;
      this.W = info.width;
      this.H = info.height;
      this.img.src = info.imgUrl;
      this.setGeometry();
    }
    this.el.classList.toggle("no-plan", !this.info);
    this.draw();
  }

  private draw() {
    const vs = this.v.viewState();
    const cams = this.v.cams;
    const traj = this.v.trajectory?.traj;
    const key = JSON.stringify([vs, cams?.shown.length, cams?.selectedId, cams?.visible, !!traj, this.v.photo?.id, this.W, this.el.clientWidth]);
    if (key === this.lastKey) return;
    this.lastKey = key;
    const k = this.k;
    const kids: SVGElement[] = [];
    if (traj && traj.p.length > 1) {
      const d = traj.p.map((p, i) => `${i ? "L" : "M"}${this.uv(p[0], p[1]).map((x) => x.toFixed(1)).join(" ")}`).join("");
      kids.push(s("path", { d, fill: "none", stroke: "#9aa0aa", "stroke-width": 1.1 * k, "stroke-dasharray": `${2 * k} ${2.5 * k}`, opacity: 0.8 }));
    }
    if (cams && cams.visible && cams.status === "ready") {
      for (const c of cams.shown) {
        const [x, y] = camCenterWorld(c);
        const [u, v] = this.uv(x, y);
        const sel = c.id === cams.selectedId;
        kids.push(
          s("circle", {
            cx: u,
            cy: v,
            r: (sel ? 5.5 : c.kind === "hero" ? 4.6 : 3.4) * k,
            fill: sel ? "#ffffff" : "#2ec4b0",
            stroke: "#0d0e10",
            "stroke-width": 0.6 * k,
            "data-cam": c.id,
            class: "cam-dot",
          }),
        );
      }
    }
    if (this.v.mode === "3d") {
      const [u, v] = this.uv(vs.pos[0], vs.pos[1]);
      const len = 1.6 * this.pxPerM;
      const a0 = vs.yaw - vs.hfov / 2;
      const a1 = vs.yaw + vs.hfov / 2;
      // World +Y is svg −v.
      const p0 = [u + Math.cos(a0) * len, v - Math.sin(a0) * len];
      const p1 = [u + Math.cos(a1) * len, v - Math.sin(a1) * len];
      kids.push(
        s("path", {
          d: `M${u} ${v} L${p0[0]} ${p0[1]} A${len} ${len} 0 0 0 ${p1[0]} ${p1[1]} Z`,
          fill: "rgba(46,196,176,0.2)",
          stroke: "rgba(46,196,176,0.7)",
          "stroke-width": 1 * k,
          class: "cone",
        }),
        s("circle", { cx: u, cy: v, r: 5.5 * k, fill: "#2ec4b0", stroke: "#fff", "stroke-width": 2 * k, class: "you" }),
      );
    } else {
      // plan mode: show the ortho viewport rectangle
      const cam = this.v.ortho;
      const [cx, cy] = [vs.pos[0], vs.pos[1]];
      const [u0, v0] = this.uv(cx + cam.left, cy + cam.top);
      const [u1, v1] = this.uv(cx + cam.right, cy + cam.bottom);
      kids.push(s("rect", { x: u0, y: v0, width: u1 - u0, height: v1 - v0, fill: "rgba(46,196,176,0.08)", stroke: "#2ec4b0", "stroke-width": 1.2 * k }));
    }
    this.dyn.replaceChildren(...kids);
  }

  private svgPoint(e: PointerEvent): [number, number] {
    const r = this.el.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * this.W, ((e.clientY - r.top) / r.height) * this.H];
  }

  private bindPointer() {
    this.el.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const [u, v] = this.svgPoint(e);
      const k = this.k;
      const cams = this.v.cams;
      if (cams?.visible) {
        let best: { id: string; d: number } | null = null;
        for (const c of cams.shown) {
          const [x, y] = camCenterWorld(c);
          const [cu, cv] = this.uv(x, y);
          const d = Math.hypot(cu - u, cv - v) / k;
          if (d < 8 && (!best || d < best.d)) best = { id: c.id, d };
        }
        if (best) {
          const cam = cams.shown.find((c) => c.id === best!.id)!;
          this.v.enterPhoto(cam);
          return;
        }
      }
      const vs = this.v.viewState();
      const [yu, yv] = this.uv(vs.pos[0], vs.pos[1]);
      const dYou = Math.hypot(yu - u, yv - v) / k;
      if (this.v.mode === "3d" && !this.v.photo && dYou > 10 && dYou < 1.7 * this.pxPerM / k && this.inCone(u, v)) {
        this.dragging = true;
        this.el.setPointerCapture(e.pointerId);
        return;
      }
      const [x, y] = this.xy(u, v);
      this.v.flyToWorldXY(x, y);
    });
    this.el.addEventListener("pointermove", (e) => {
      if (!this.dragging) return;
      const [u, v] = this.svgPoint(e);
      const [x, y] = this.xy(u, v);
      this.v.lookTowardWorldXY(x, y);
    });
    this.el.addEventListener("pointerup", () => (this.dragging = false));
  }

  private inCone(u: number, v: number): boolean {
    const vs = this.v.viewState();
    const [yu, yv] = this.uv(vs.pos[0], vs.pos[1]);
    const a = Math.atan2(-(v - yv), u - yu);
    let d = a - vs.yaw;
    d = Math.atan2(Math.sin(d), Math.cos(d));
    return Math.abs(d) < vs.hfov / 2 + 0.15;
  }
}
