import * as THREE from "three";
import { Layer, type LayerCtx } from "./base";
import type { LayerDef } from "../types";

export interface PlanInfo {
  styleId: string;
  svgText: string;
  /** Blob URL of the SVG, safe for <img>. */
  imgUrl: string;
  bounds: [number, number, number, number]; // minx miny maxx maxy (World)
  pxPerM: number;
  width: number;
  height: number;
}

export function parsePlanSvg(svgText: string): Omit<PlanInfo, "styleId" | "imgUrl" | "svgText"> {
  const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const svg = doc.documentElement;
  const b = (svg.getAttribute("data-world-bounds") || "").trim().split(/[\s,]+/).map(Number);
  if (b.length !== 4 || b.some((x) => !Number.isFinite(x))) throw new Error("plan svg missing data-world-bounds");
  let pxPerM = Number(svg.getAttribute("data-px-per-m"));
  let width = parseFloat(svg.getAttribute("width") || "");
  let height = parseFloat(svg.getAttribute("height") || "");
  const vb = (svg.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
  if (!Number.isFinite(width) && vb.length === 4) width = vb[2];
  if (!Number.isFinite(height) && vb.length === 4) height = vb[3];
  if (!Number.isFinite(pxPerM) || pxPerM <= 0) pxPerM = width / (b[2] - b[0]);
  return { bounds: [b[0], b[1], b[2], b[3]], pxPerM, width, height };
}

/** Plan SVG laid on the floor plane (World z = floor), per data-world-bounds. */
export class PlanLayer extends Layer {
  info?: PlanInfo;
  private mesh?: THREE.Mesh;
  private cache = new Map<string, PlanInfo>();

  constructor(def: LayerDef, ctx: LayerCtx) {
    super(def, ctx);
    this.mode = def.style ?? def.styles?.[0]?.id;
  }

  get modes() {
    return (this.def.styles ?? []).map((s) => s.id);
  }

  protected async load() {
    await this.loadStyle(this.mode!);
  }

  protected applyMode() {
    if (this.mode && this.info?.styleId !== this.mode) void this.loadStyle(this.mode).then(() => this.ctx.changed());
  }

  async fetchStyle(styleId: string): Promise<PlanInfo> {
    const cached = this.cache.get(styleId);
    if (cached) return cached;
    const style = this.def.styles?.find((s) => s.id === styleId) ?? this.def.styles?.[0];
    if (!style) throw new Error("plan layer has no styles");
    const svgText = await this.ctx.fetcher.text(this.ctx.resolve(style.url));
    const meta = parsePlanSvg(svgText);
    const imgUrl = URL.createObjectURL(new Blob([svgText], { type: "image/svg+xml" }));
    const info = { ...meta, styleId: style.id, svgText, imgUrl };
    this.cache.set(style.id, info);
    return info;
  }

  private async loadStyle(styleId: string) {
    const info = await this.fetchStyle(styleId);
    // The floor texture omits the north arrow / scale bar (they belong to the minimap card).
    const doc = new DOMParser().parseFromString(info.svgText, "image/svg+xml");
    doc.querySelectorAll(".annotations").forEach((n) => n.remove());
    const floorUrl = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(doc)], { type: "image/svg+xml" }));
    const img = new Image();
    img.decoding = "async";
    img.src = floorUrl;
    try {
      await img.decode();
    } finally {
      URL.revokeObjectURL(floorUrl);
    }
    const maxPx = Math.min(4096, this.ctx.renderer.capabilities.maxTextureSize);
    const scale = Math.min(2, maxPx / Math.max(info.width, info.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(info.width * scale);
    canvas.height = Math.round(info.height * scale);
    const g = canvas.getContext("2d")!;
    g.drawImage(img, 0, 0, canvas.width, canvas.height);
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = this.ctx.renderer.capabilities.getMaxAnisotropy();
    const [x0, y0, x1, y1] = info.bounds;
    if (this.mesh) {
      this.object.remove(this.mesh);
      this.mesh.geometry.dispose();
      ((this.mesh.material as THREE.MeshBasicMaterial).map as THREE.Texture | null)?.dispose();
    }
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(x1 - x0, y1 - y0),
      new THREE.MeshBasicMaterial({ map: tex, toneMapped: false, polygonOffset: true, polygonOffsetFactor: 2, polygonOffsetUnits: 2 }),
    );
    const floor = this.ctx.scene.levels?.floor ?? 0;
    mesh.position.set((x0 + x1) / 2, (y0 + y1) / 2, floor - 0.045);
    mesh.renderOrder = -5;
    this.object.add(mesh);
    this.mesh = mesh;
    this.info = info;
    this.applyOverlay();
  }

  /** In plan view the plan is drawn over the splat floor (slightly translucent). */
  setOverlay(on: boolean) {
    this.overlay = on;
    this.applyOverlay();
  }

  private overlay = false;

  private applyOverlay() {
    if (!this.mesh) return;
    const m = this.mesh.material as THREE.MeshBasicMaterial;
    m.transparent = this.overlay;
    m.opacity = this.overlay ? 0.9 : 1;
    m.depthTest = !this.overlay;
    m.depthWrite = !this.overlay;
    m.needsUpdate = true;
    this.mesh.renderOrder = this.overlay ? 50 : -5;
  }

  styleLabel(): string {
    const ids = this.modes;
    const i = Math.max(0, ids.indexOf(this.mode ?? ""));
    return `${i + 1} of ${ids.length}`;
  }

  summary() {
    if (this.status === "error") return "failed";
    const s = this.mode ? this.mode[0].toUpperCase() + this.mode.slice(1) : "";
    return `${s} plan`;
  }

  statusLine() {
    const i = this.info;
    return i
      ? `Plan · ${i.styleId} · ${(i.bounds[2] - i.bounds[0]).toFixed(1)} × ${(i.bounds[3] - i.bounds[1]).toFixed(1)} m at ${i.pxPerM} px/m`
      : "Plan";
  }

  pickables() {
    return this.mesh ? [this.mesh] : [];
  }
}

const GROUND_VERT = /* glsl */ `
varying vec2 vXY;
varying vec3 vWorld;
void main() {
  vXY = position.xy;
  vec4 w = modelMatrix * vec4(position, 1.0);
  vWorld = w.xyz;
  gl_Position = projectionMatrix * viewMatrix * w;
}`;

const GROUND_FRAG = /* glsl */ `
uniform float uCell;
uniform float uFade;
uniform float uChecker;
uniform vec4 uBounds;
uniform vec3 uColor;
varying vec2 vXY;
varying vec3 vWorld;
void main() {
  vec2 p = vXY / uCell;
  vec2 fw = fwidth(p);
  vec2 g = abs(fract(p - 0.5) - 0.5) / max(fw, 1e-4);
  float line = 1.0 - min(min(g.x, g.y), 1.0);
  // lines thin out where they'd alias
  line *= 1.0 - smoothstep(0.25, 0.6, max(fw.x, fw.y));
  float chk = mod(floor(p.x) + floor(p.y), 2.0);
  vec2 d = max(max(uBounds.xy - vXY, vXY - uBounds.zw), 0.0);
  float fade = 1.0 - smoothstep(uFade, uFade * 6.0, length(d));
  float camFade = 1.0 - smoothstep(25.0, 70.0, length(cameraPosition - vWorld));
  float a = (uChecker > 0.5 ? 0.06 + 0.07 * chk : 0.75 * line) * fade * camFade;
  if (a < 0.003) discard;
  gl_FragColor = vec4(uColor, a);
}`;

/** Infinite-ish ground grid: lines | checker, cell size, fade distance. */
export class GroundLayer extends Layer {
  private mat: THREE.ShaderMaterial;

  constructor(def: LayerDef, ctx: LayerCtx) {
    super(def, ctx);
    this.mode = def.mode ?? "lines";
    const b = ctx.scene.bounds;
    this.mat = new THREE.ShaderMaterial({
      vertexShader: GROUND_VERT,
      fragmentShader: GROUND_FRAG,
      transparent: true,
      depthWrite: false,
      extensions: { derivatives: true } as never,
      uniforms: {
        uCell: { value: def.cell ?? 0.5 },
        uFade: { value: def.fade ?? 4 },
        uChecker: { value: 0 },
        uBounds: { value: new THREE.Vector4(b.min[0], b.min[1], b.max[0], b.max[1]) },
        uColor: { value: new THREE.Color(0x8a8d96) },
      },
    });
  }

  get modes() {
    return this.def.modes ?? ["lines", "checker"];
  }

  protected async load() {
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(400, 400), this.mat);
    mesh.position.z = (this.ctx.scene.levels?.floor ?? 0) - 0.07;
    mesh.renderOrder = -10;
    mesh.raycast = () => {};
    this.object.add(mesh);
  }

  protected applyMode() {
    this.mat.uniforms.uChecker.value = this.mode === "checker" ? 1 : 0;
  }

  get cell(): number {
    return this.mat.uniforms.uCell.value;
  }

  summary() {
    return `${this.cell} m cells · fades ${this.mat.uniforms.uFade.value} m`;
  }

  statusLine() {
    return `Ground · ${this.mode} · ${this.cell} m cells, fades ${this.mat.uniforms.uFade.value} m`;
  }
}
