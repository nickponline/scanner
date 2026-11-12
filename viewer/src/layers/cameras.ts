import * as THREE from "three";
import { Layer, type LayerCtx } from "./base";
import { camCenterWorld } from "../frames";
import type { CameraDef, LayerDef, Trajectory } from "../types";

const MARKER_VERT = /* glsl */ `
attribute float aSize;
attribute vec3 aColor;
uniform float uPixelRatio;
varying vec3 vColor;
void main() {
  vColor = aColor;
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = aSize * uPixelRatio;
}`;

const MARKER_FRAG = /* glsl */ `
varying vec3 vColor;
void main() {
  vec2 c = gl_PointCoord - 0.5;
  float r = length(c) * 2.0;
  if (r > 1.0) discard;
  float aa = fwidth(r);
  vec3 col = mix(vColor, vec3(1.0), smoothstep(0.66 - aa, 0.66 + aa, r));
  float alpha = 1.0 - smoothstep(1.0 - aa * 1.5, 1.0, r);
  gl_FragColor = vec4(col, alpha);
  #include <colorspace_fragment>
}`;

export const MARKER_RED = new THREE.Color("#2ec4b0");
const MARKER_SEL = new THREE.Color("#ffffff");

/** cameras.json: red station dots, selectable, filterable by keyframes | hero | all. */
export class CamerasLayer extends Layer {
  cameras: CameraDef[] = [];
  shown: CameraDef[] = [];
  selectedId: string | null = null;
  private assetBase = "";
  private points?: THREE.Points;
  private frustum = new THREE.LineSegments(
    new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.95, depthTest: false }),
  );
  private mat = new THREE.ShaderMaterial({
    vertexShader: MARKER_VERT,
    fragmentShader: MARKER_FRAG,
    uniforms: { uPixelRatio: { value: Math.min(window.devicePixelRatio, 2) } },
    transparent: true,
    depthTest: false,
    depthWrite: false,
  });

  constructor(def: LayerDef, ctx: LayerCtx) {
    super(def, ctx);
    this.mode = def.mode ?? "keyframes";
    this.frustum.renderOrder = 61;
    this.frustum.visible = false;
    this.object.add(this.frustum);
  }

  protected async load() {
    const url = this.ctx.resolve(this.def.url!);
    const json = await this.ctx.fetcher.json<{ cameras: CameraDef[] }>(url);
    // Relative photo paths resolve against the URL of cameras.json itself (CONTRACTS §4).
    this.assetBase = url;
    this.cameras = [...(json.cameras ?? [])].sort((a, b) => a.t - b.t);
  }

  photoUrl(c: CameraDef, which: "full" | "thumb"): string {
    return new URL(c[which], this.assetBase).href;
  }

  protected applyMode() {
    const m = this.mode;
    this.shown = this.cameras.filter((c) =>
      m === "hero" ? c.kind === "hero" : m === "keyframes" ? c.kind !== "hero" : true,
    );
    if (this.shown.length === 0) this.shown = this.cameras;
    this.rebuild();
  }

  private rebuild() {
    if (this.points) {
      this.object.remove(this.points);
      this.points.geometry.dispose();
    }
    const n = this.shown.length;
    const pos = new Float32Array(n * 3);
    const col = new Float32Array(n * 3);
    const size = new Float32Array(n);
    this.shown.forEach((c, i) => {
      const [x, y, z] = camCenterWorld(c);
      pos.set([x, y, z], i * 3);
      const sel = c.id === this.selectedId;
      (sel ? MARKER_SEL : MARKER_RED).toArray(col, i * 3);
      size[i] = sel ? 17 : c.kind === "hero" ? 15 : 12;
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setAttribute("aColor", new THREE.BufferAttribute(col, 3));
    g.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
    this.points = new THREE.Points(g, this.mat);
    this.points.renderOrder = 60;
    this.points.visible = !this.selectedId;
    this.points.frustumCulled = false;
    this.points.raycast = () => {};
    this.object.add(this.points);
  }

  select(id: string | null) {
    this.selectedId = id;
    const c = this.cameras.find((x) => x.id === id);
    this.frustum.visible = !!c;
    if (c) this.frustum.geometry = frustumGeometry(c, 0.35);
    if (this.status === "ready") this.rebuild();
  }

  /** Pick the shown camera nearest the screen point (CSS px), within `radius` px. */
  pick(camera: THREE.Camera, x: number, y: number, W: number, H: number, radius = 12): CameraDef | null {
    if (!this.visible || this.status !== "ready") return null;
    let best: CameraDef | null = null;
    let bestD = radius * radius;
    const v = new THREE.Vector3();
    for (const c of this.shown) {
      const [wx, wy, wz] = camCenterWorld(c);
      v.set(wx, wy, wz).applyMatrix4(this.ctx.root.matrixWorld).project(camera);
      if (v.z > 1 || v.z < -1) continue;
      const sx = ((v.x + 1) / 2) * W;
      const sy = ((1 - v.y) / 2) * H;
      const d = (sx - x) ** 2 + (sy - y) ** 2;
      if (d < bestD) {
        bestD = d;
        best = c;
      }
    }
    return best;
  }

  statusLine() {
    const hero = this.cameras.filter((c) => c.kind === "hero").length;
    return `Photos · ${this.cameras.length} posed photos (${hero} hero), showing ${this.shown.length} · click a dot to view (${(this.loadSeconds ?? 0).toFixed(1)} s)`;
  }
}

function frustumGeometry(c: CameraDef, depth: number): THREE.BufferGeometry {
  const M = c.c2w;
  const toW = (u: number, v: number, d: number) => {
    const x = ((u - c.cx) / c.fx) * d;
    const y = ((v - c.cy) / c.fy) * d;
    const z = d;
    return [
      M[0] * x + M[1] * y + M[2] * z + M[3],
      M[4] * x + M[5] * y + M[6] * z + M[7],
      M[8] * x + M[9] * y + M[10] * z + M[11],
    ];
  };
  const o = [M[3], M[7], M[11]];
  const cs = [toW(0, 0, depth), toW(c.w, 0, depth), toW(c.w, c.h, depth), toW(0, c.h, depth)];
  const seg: number[] = [];
  for (let i = 0; i < 4; i++) seg.push(...o, ...cs[i], ...cs[i], ...cs[(i + 1) % 4]);
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(seg, 3));
  return g;
}

/** Walked path polyline. */
export class TrajectoryLayer extends Layer {
  traj?: Trajectory;

  protected async load() {
    const t = await this.ctx.fetcher.json<Trajectory>(this.ctx.resolve(this.def.url!));
    this.traj = t;
    const g = new THREE.BufferGeometry().setFromPoints(t.p.map((p) => new THREE.Vector3(p[0], p[1], p[2])));
    const line = new THREE.Line(
      g,
      new THREE.LineDashedMaterial({ color: 0xffffff, dashSize: 0.12, gapSize: 0.08, transparent: true, opacity: 0.7 }),
    );
    line.computeLineDistances();
    line.renderOrder = 10;
    this.object.add(line);
  }

  statusLine() {
    const n = this.traj?.p.length ?? 0;
    const dur = this.traj?.t.length ? this.traj.t[this.traj.t.length - 1] - this.traj.t[0] : 0;
    return `Trajectory · ${n} samples, ${dur.toFixed(0)} s`;
  }
}
