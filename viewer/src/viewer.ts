import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { SparkRenderer } from "@sparkjsdev/spark";
import type { LoadedScene } from "./data";
import { ROOT_MATRIX, intrinsicsProjection, letterboxFov, poseFromC2W, t2w, w2t } from "./frames";
import type { BuildingModel, CameraDef, SceneJson } from "./types";
import { Layer, type ClipGroup, type LayerCtx } from "./layers/base";
import { CadLayer, MeshLayer } from "./layers/models";
import { SplatLayer } from "./layers/splat";

/** Gaussian splat display is switched off for now; flip to true to bring it back. */
export const SPLAT_ENABLED = false;
import { PointsLayer } from "./layers/points";
import { CamerasLayer, TrajectoryLayer } from "./layers/cameras";
import { GroundLayer, PlanLayer } from "./layers/plan";

export type ViewMode = "3d" | "plan";
export const EYE_HEIGHT = 1.6;

/** True for focused elements that consume typing (not sliders/checkboxes). */
export function isTextEntry(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el) return false;
  if (el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable) return true;
  if (el.tagName === "INPUT") return !["range", "checkbox", "radio", "button"].includes((el as HTMLInputElement).type);
  return false;
}

interface Tween {
  t0: number;
  dur: number;
  step(k: number): void;
  done?: () => void;
}

const ease = (k: number) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);

export class Viewer extends EventTarget {
  readonly scene: SceneJson;
  readonly renderer: THREE.WebGLRenderer;
  readonly scene3 = new THREE.Scene();
  readonly root = new THREE.Group();
  readonly persp: THREE.PerspectiveCamera;
  readonly ortho: THREE.OrthographicCamera;
  readonly controls: OrbitControls;
  readonly spark: SparkRenderer;
  readonly layers: Layer[] = [];
  readonly clipPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 1.2);
  readonly clipTargets: Record<ClipGroup, boolean> = { splat: true, model: true, mesh: true, points: true };
  cut = { on: false, z: 1.2 };
  mode: ViewMode = "3d";
  walking = false;
  photo: CameraDef | null = null;
  activeLayerId: string | null = null;
  buildingModel: Promise<BuildingModel | null>;
  stats = { fps: 0, calls: 0, tris: 0 };

  private tweens: Tween[] = [];
  private keys = new Set<string>();
  private clock = new THREE.Clock();
  private frames = 0;
  private fpsT = 0;
  private planCenter = new THREE.Vector3();
  private planZoom = 1;
  private cutBeforePlan: boolean | null = null;
  private changeQueued = false;
  private sceneHeight: number;

  constructor(
    public container: HTMLElement,
    public loaded: LoadedScene,
  ) {
    super();
    this.scene = loaded.scene;
    const W = container.clientWidth || 800;
    const H = container.clientHeight || 600;
    this.renderer = new THREE.WebGLRenderer({ antialias: false, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(W, H);
    this.renderer.setClearColor(0x111214, 1);
    this.renderer.localClippingEnabled = true;
    container.appendChild(this.renderer.domElement);
    this.renderer.domElement.classList.add("gl");

    this.root.matrixAutoUpdate = false;
    this.root.matrix.copy(ROOT_MATRIX);
    this.root.name = "world-root";
    this.scene3.add(this.root);
    this.scene3.add(new THREE.HemisphereLight(0xffffff, 0x50525a, 2.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(4, 10, 6);
    this.scene3.add(sun);

    this.spark = new SparkRenderer({ renderer: this.renderer });
    this.scene3.add(this.spark);

    this.persp = new THREE.PerspectiveCamera(45, W / H, 0.03, 500);
    this.ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 500);
    this.controls = new OrbitControls(this.persp, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.screenSpacePanning = true;
    this.controls.maxPolarAngle = Math.PI * 0.495;

    const lv = this.scene.levels;
    this.sceneHeight = Math.max(this.scene.bounds.max[2], (lv?.ceiling_max ?? lv?.ceiling ?? 2.5) + 0.3);
    // Default: dollhouse view, cut just under the (lowest) ceiling like the reference. ?cut=off disables.
    if (lv) {
      this.setCutZ(Math.round((lv.ceiling - 0.05) * 100) / 100, false);
      this.cut.on = true;
    } else this.setCutZ(1.2, false);
    this.resetView(false);

    this.buildingModel = this.scene.building_model_url
      ? loaded.fetcher.json<BuildingModel>(this.resolve(this.scene.building_model_url)).catch(() => null)
      : Promise.resolve(null);
    this.createLayers();

    this.bindInput();
    new ResizeObserver(() => this.resize()).observe(container);
    this.renderer.setAnimationLoop(() => this.frame());
  }

  resolve(url: string) {
    return new URL(url, this.loaded.base).href;
  }

  // ------------------------------------------------------------------ layers

  private createLayers() {
    const ctx: LayerCtx = {
      root: this.root,
      scene3: this.scene3,
      renderer: this.renderer,
      spark: this.spark,
      fetcher: this.loaded.fetcher,
      scene: this.scene,
      resolve: (u) => this.resolve(u),
      buildingModel: this.buildingModel,
      clipPlane: this.clipPlane,
      changed: () => this.changed(),
    };
    // The Argon agent's model parts (architecture / furniture / ceilings) are presented as one CAD layer.
    const modelDefs = (this.scene.layers ?? []).filter((d) => d.kind === "model" && d.url);
    let cadAdded = false;
    // With a splat, open on the splat alone: the translucent CAD solids and the ground grid don't write depth
    // over it (splats don't write depth), so they veil the whole image in grey. Both stay one toggle away.
    // Gaussian splat is disabled for now: splat layers in scene.json are ignored (SPLAT_ENABLED to restore).
    if (!SPLAT_ENABLED) this.scene.layers = (this.scene.layers ?? []).filter((d) => d.kind !== "splat");
    const hasSplat = (this.scene.layers ?? []).some((d) => d.kind === "splat" && d.visible !== false && d.url);
    for (const sceneDef of this.scene.layers ?? []) {
      const def = hasSplat && sceneDef.kind === "ground" ? { ...sceneDef, visible: false } : sceneDef;
      let layer: Layer | null = null;
      switch (def.kind) {
        case "model": {
          if (cadAdded || !def.url) continue;
          cadAdded = true;
          const parts = modelDefs.map((d) => ({ id: d.id, url: d.url! }));
          const anyVisible = !hasSplat && modelDefs.some((d) => d.visible !== false && d.id !== "ceilings");
          const cadDef = { id: "cad", kind: "model", label: "CAD", url: parts[0].url, visible: anyVisible, mode: "solid", summary: "Argon" };
          layer = new CadLayer(cadDef, ctx, parts);
          break;
        }
        case "mesh":
          layer = new MeshLayer(def, ctx);
          break;
        case "splat":
          layer = new SplatLayer(def, ctx);
          break;
        case "points":
          layer = new PointsLayer(def, ctx);
          break;
        case "cameras":
          layer = new CamerasLayer(def, ctx);
          break;
        case "trajectory":
          layer = new TrajectoryLayer(def, ctx);
          break;
        case "plan":
          layer = new PlanLayer(def, ctx);
          break;
        case "ground":
          layer = new GroundLayer(def, ctx);
          break;
        default:
          console.warn(`unknown layer kind ${def.kind} (${def.id}); skipped`);
      }
      if (!layer) continue;
      if (!def.url && !["plan", "ground"].includes(def.kind)) {
        console.warn(`layer ${def.id} has no url; skipped`);
        this.root.remove(layer.object);
        continue;
      }
      this.layers.push(layer);
      // Visible layers load now; small data layers always (minimap needs them).
      if (layer.visible || def.kind === "cameras" || def.kind === "trajectory" || def.kind === "plan") void layer.ensureLoaded();
    }
    // Push the current cut height into layers created after the constructor set it (splat SDF plane).
    this.setCutZ(this.cut.z, false);
    this.activeLayerId = (this.layers.find((l) => l.def.kind === "splat" && l.visible) ?? this.layers.find((l) => l.visible))?.id ?? null;
    this.applyClipping();
  }

  layer<T extends Layer = Layer>(id: string): T | undefined {
    return this.layers.find((l) => l.id === id) as T | undefined;
  }

  layerOfKind<T extends Layer = Layer>(kind: string): T | undefined {
    return this.layers.find((l) => l.def.kind === kind) as T | undefined;
  }

  get splat(): SplatLayer | undefined {
    return this.layerOfKind<SplatLayer>("splat");
  }

  get cams(): CamerasLayer | undefined {
    return this.layerOfKind<CamerasLayer>("cameras");
  }

  get plan(): PlanLayer | undefined {
    return this.layerOfKind<PlanLayer>("plan");
  }

  get trajectory(): TrajectoryLayer | undefined {
    return this.layerOfKind<TrajectoryLayer>("trajectory");
  }

  setActiveLayer(id: string) {
    this.activeLayerId = id;
    this.changed();
  }

  /** Coalesced "something changed" notification for UI. */
  changed() {
    if (this.changeQueued) return;
    this.changeQueued = true;
    queueMicrotask(() => {
      this.changeQueued = false;
      this.dispatchEvent(new Event("change"));
    });
  }

  // ------------------------------------------------------------------ section cut

  get cutRange(): [number, number] {
    return [Math.min(this.scene.bounds.min[2], this.scene.levels?.floor ?? 0), this.sceneHeight];
  }

  setCutZ(z: number, notify = true) {
    const [lo, hi] = this.cutRange;
    this.cut.z = THREE.MathUtils.clamp(z, lo, hi);
    // Plane lives in three world space: World z → three y. Keep y <= z.
    this.clipPlane.set(new THREE.Vector3(0, -1, 0), this.cut.z);
    this.splat?.setCut(this.cut.z);
    if (notify) this.changed();
  }

  setCutOn(on: boolean) {
    this.cut.on = on;
    this.applyClipping();
    this.changed();
  }

  setClipTarget(g: ClipGroup, on: boolean) {
    this.clipTargets[g] = on;
    this.applyClipping();
    this.changed();
  }

  private applyClipping() {
    for (const l of this.layers) if (l.clipGroup) l.setClipped(this.cut.on && this.clipTargets[l.clipGroup]);
  }

  // ------------------------------------------------------------------ views

  get camera(): THREE.Camera {
    return this.mode === "plan" ? this.ortho : this.persp;
  }

  resetView(animate = true) {
    const dv = this.scene.default_view;
    const b = this.scene.bounds;
    const target = dv ? w2t(dv.target[0], dv.target[1], dv.target[2]) : w2t((b.min[0] + b.max[0]) / 2, (b.min[1] + b.max[1]) / 2, 0.5);
    const pos = dv
      ? w2t(dv.position[0], dv.position[1], dv.position[2])
      : target.clone().add(new THREE.Vector3(0.35, 1.15, 0.5).multiplyScalar(Math.max(b.max[0] - b.min[0], b.max[1] - b.min[1])));
    if (animate) this.flyTo(pos, target);
    else {
      this.persp.position.copy(pos);
      this.controls.target.copy(target);
      this.controls.update();
    }
  }

  /** Animate orbit camera to position/target (three frame). */
  flyTo(pos: THREE.Vector3, target: THREE.Vector3, dur = 900, done?: () => void) {
    const p0 = this.persp.position.clone();
    const t0 = this.controls.target.clone();
    const f0 = this.persp.fov;
    this.tweens = [];
    this.addTween(dur, (k) => {
      this.persp.position.lerpVectors(p0, pos, k);
      this.controls.target.lerpVectors(t0, target, k);
      this.persp.fov = f0 + (45 - f0) * k;
      this.persp.updateProjectionMatrix();
      this.persp.lookAt(this.controls.target);
    }, done);
  }

  private addTween(dur: number, step: (k: number) => void, done?: () => void) {
    this.tweens.push({ t0: performance.now(), dur, step, done });
  }

  setMode(mode: ViewMode) {
    if (mode === this.mode) return;
    if (this.photo) this.exitPhoto();
    if (this.walking) this.exitWalk();
    this.mode = mode;
    if (mode === "plan") {
      const b = this.scene.bounds;
      this.planCenter.copy(w2t((b.min[0] + b.max[0]) / 2, (b.min[1] + b.max[1]) / 2, 0));
      this.planZoom = 1;
      this.controls.enabled = false;
      this.plan?.setOverlay(true);
      this.cutBeforePlan = this.cut.on;
      if (!this.cut.on) {
        this.setCutZ(this.scene.levels ? Math.min(1.2, this.scene.levels.ceiling - 0.3) : 1.2, false);
        this.setCutOn(true);
      }
      this.updateOrtho();
    } else {
      this.controls.enabled = true;
      this.plan?.setOverlay(false);
      if (this.cutBeforePlan === false) this.setCutOn(false);
      this.cutBeforePlan = null;
    }
    this.changed();
  }

  private updateOrtho() {
    const W = this.container.clientWidth || 1;
    const H = this.container.clientHeight || 1;
    const b = this.scene.bounds;
    const sw = b.max[0] - b.min[0];
    const sh = b.max[1] - b.min[1];
    const aspect = W / H;
    let halfH = (Math.max(sh, sw / aspect) / 2) * 1.25;
    halfH /= this.planZoom;
    this.ortho.left = -halfH * aspect;
    this.ortho.right = halfH * aspect;
    this.ortho.top = halfH;
    this.ortho.bottom = -halfH;
    this.ortho.position.set(this.planCenter.x, this.sceneHeight + 20, this.planCenter.z);
    this.ortho.up.set(0, 0, -1);
    this.ortho.lookAt(this.planCenter.x, 0, this.planCenter.z);
    this.ortho.updateProjectionMatrix();
  }

  /** World-frame view state of the active camera (for minimap and deep links). */
  viewState(): { pos: [number, number, number]; target: [number, number, number]; yaw: number; hfov: number } {
    if (this.mode === "plan") {
      const c = t2w(this.planCenter);
      return { pos: [c[0], c[1], this.sceneHeight + 20], target: [c[0], c[1], 0], yaw: Math.PI / 2, hfov: 0 };
    }
    const dir = new THREE.Vector3();
    this.persp.getWorldDirection(dir);
    const dw = t2w(dir);
    const hfov = 2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(this.persp.fov) / 2) * this.persp.aspect);
    const target = this.photo || this.walking ? t2w(this.persp.position.clone().add(dir)) : t2w(this.controls.target);
    return { pos: t2w(this.persp.position), target, yaw: Math.atan2(dw[1], dw[0]), hfov };
  }

  /** Apply a deep-link camera (World frame). */
  setView(pos: number[], target: number[], animate = false) {
    const p = w2t(pos[0], pos[1], pos[2]);
    const t = w2t(target[0], target[1], target[2]);
    if (animate) this.flyTo(p, t);
    else {
      this.persp.position.copy(p);
      this.controls.target.copy(t);
      this.controls.update();
    }
  }

  /** Fly so the orbit target moves to World (x, y) keeping the current offset (minimap click). */
  flyToWorldXY(x: number, y: number) {
    if (this.photo) this.exitPhoto();
    if (this.mode === "plan") {
      this.planCenter.copy(w2t(x, y, 0));
      this.updateOrtho();
      this.changed();
      return;
    }
    if (this.walking) {
      const p = w2t(x, y, (this.scene.levels?.floor ?? 0) + EYE_HEIGHT);
      const off = this.controls.target.clone().sub(this.persp.position);
      this.flyTo(p, p.clone().add(off), 700);
      return;
    }
    const target = w2t(x, y, this.controls.target.y);
    target.y = this.controls.target.y;
    const off = this.persp.position.clone().sub(this.controls.target);
    this.flyTo(target.clone().add(off), target);
  }

  /** Rotate the view azimuth so it looks toward World (x, y) (minimap cone drag). */
  lookTowardWorldXY(x: number, y: number) {
    if (this.mode === "plan" || this.photo) return;
    const target = w2t(x, y, 0);
    if (this.walking) {
      const d = target.clone().sub(this.persp.position).setY(0).normalize();
      this.controls.target.copy(this.persp.position).add(d.multiplyScalar(0.05));
    } else {
      const c = this.controls.target;
      const off = this.persp.position.clone().sub(c);
      const dist = Math.hypot(off.x, off.z);
      const d = target.clone().sub(c).setY(0);
      if (d.lengthSq() < 1e-6) return;
      d.normalize();
      this.persp.position.set(c.x - d.x * dist, this.persp.position.y, c.z - d.z * dist);
    }
    this.controls.update();
    this.changed();
  }

  // ------------------------------------------------------------------ photo click-through

  enterPhoto(cam: CameraDef) {
    if (this.mode === "plan") this.setMode("3d");
    if (this.walking) this.exitWalk();
    this.photo = cam;
    this.cams?.select(cam.id);
    this.controls.enabled = false;
    const { position, quaternion } = poseFromC2W(cam.c2w);
    const W = this.container.clientWidth;
    const H = this.container.clientHeight;
    const fov1 = letterboxFov(cam, W, H);
    const p0 = this.persp.position.clone();
    const q0 = this.persp.quaternion.clone();
    const f0 = this.persp.fov;
    this.tweens = [];
    this.addTween(
      850,
      (k) => {
        this.persp.position.lerpVectors(p0, position, k);
        this.persp.quaternion.slerpQuaternions(q0, quaternion, k);
        this.persp.fov = f0 + (fov1 - f0) * k;
        this.persp.updateProjectionMatrix();
      },
      () => this.applyPhotoProjection(),
    );
    this.dispatchEvent(new CustomEvent("photo", { detail: cam }));
    this.changed();
  }

  /** Exact intrinsics (incl. principal point) for the letterboxed photo rect. */
  private applyPhotoProjection() {
    if (!this.photo) return;
    const W = this.container.clientWidth;
    const H = this.container.clientHeight;
    this.persp.projectionMatrix.copy(intrinsicsProjection(this.photo, W, H, this.persp.near, this.persp.far));
    this.persp.projectionMatrixInverse.copy(this.persp.projectionMatrix).invert();
  }

  stepPhoto(delta: number) {
    const cams = this.cams?.shown ?? [];
    if (!cams.length) return;
    const i = this.photo ? cams.findIndex((c) => c.id === this.photo!.id) : -1;
    const j = (i + delta + cams.length) % cams.length;
    this.enterPhoto(cams[j]);
  }

  exitPhoto() {
    if (!this.photo) return;
    const dir = new THREE.Vector3();
    this.persp.getWorldDirection(dir);
    this.photo = null;
    this.cams?.select(null);
    this.tweens = [];
    this.persp.updateProjectionMatrix();
    this.controls.target.copy(this.persp.position).add(dir.multiplyScalar(2.5));
    this.controls.enabled = true;
    this.controls.update();
    this.dispatchEvent(new CustomEvent("photo", { detail: null }));
    this.changed();
  }

  // ------------------------------------------------------------------ walk mode

  enterWalk() {
    if (this.photo) this.exitPhoto();
    if (this.mode === "plan") this.setMode("3d");
    this.walking = true;
    const b = this.scene.bounds;
    const floor = this.scene.levels?.floor ?? 0;
    const tw = t2w(this.controls.target);
    const cw = t2w(this.persp.position);
    const inside = cw[0] > b.min[0] && cw[0] < b.max[0] && cw[1] > b.min[1] && cw[1] < b.max[1];
    let start: number[] = inside ? cw : tw;
    let dir = this.controls.target.clone().sub(this.persp.position).setY(0);
    // Drop in at the nearest photo station (a known free-standing spot), facing its view direction.
    const cams = this.cams?.status === "ready" ? this.cams.cameras : [];
    if (!inside && cams.length) {
      let best = cams[0];
      let bd = Infinity;
      for (const c of cams) {
        const d = (c.c2w[3] - tw[0]) ** 2 + (c.c2w[7] - tw[1]) ** 2;
        if (d < bd) {
          bd = d;
          best = c;
        }
      }
      start = [best.c2w[3], best.c2w[7]];
      dir = w2t(best.c2w[2], best.c2w[6], 0);
    }
    const p = w2t(start[0], start[1], floor + EYE_HEIGHT);
    if (dir.lengthSq() < 1e-6) dir.set(0, 0, -1);
    dir.normalize();
    this.controls.enableZoom = false;
    this.controls.enablePan = false;
    this.controls.rotateSpeed = -0.35;
    this.controls.maxPolarAngle = Math.PI * 0.85;
    this.controls.minPolarAngle = Math.PI * 0.15;
    this.flyTo(p, p.clone().add(dir.multiplyScalar(0.05)), 700);
    this.changed();
  }

  exitWalk() {
    if (!this.walking) return;
    this.walking = false;
    this.controls.enableZoom = true;
    this.controls.enablePan = true;
    this.controls.rotateSpeed = 1;
    this.controls.maxPolarAngle = Math.PI * 0.495;
    this.controls.minPolarAngle = 0;
    const dir = this.controls.target.clone().sub(this.persp.position).normalize();
    this.controls.target.copy(this.persp.position).add(dir.multiplyScalar(3));
    this.controls.update();
    this.changed();
  }

  private walkStep(dt: number) {
    if (this.mode !== "3d" || this.tweens.length) return;
    const up = (this.keys.has("e") ? 1 : 0) - (this.keys.has("q") ? 1 : 0);
    if (up) {
      const dy = up * (this.keys.has("shift") ? 2.4 : 1.2) * dt;
      this.persp.position.y += dy;
      this.controls.target.y += dy;
      this.changed();
    }
    if (!this.walking) return;
    let fwd = 0;
    let side = 0;
    if (this.keys.has("w") || this.keys.has("arrowup")) fwd += 1;
    if (this.keys.has("s") || this.keys.has("arrowdown")) fwd -= 1;
    if (this.keys.has("d")) side += 1;
    if (this.keys.has("a")) side -= 1;
    if (!fwd && !side) return;
    const speed = (this.keys.has("shift") ? 2.8 : 1.4) * dt;
    const dir = this.controls.target.clone().sub(this.persp.position).setY(0).normalize();
    const right = new THREE.Vector3().crossVectors(dir, new THREE.Vector3(0, 1, 0));
    const move = dir.multiplyScalar(fwd * speed).add(right.multiplyScalar(side * speed));
    this.persp.position.add(move);
    this.controls.target.add(move);
    this.changed();
  }

  // ------------------------------------------------------------------ picking

  /** Screen point (CSS px relative to container) → World point on a surface or floor. */
  pickWorld(x: number, y: number): THREE.Vector3 | null {
    const W = this.container.clientWidth;
    const H = this.container.clientHeight;
    const ndc = new THREE.Vector2((x / W) * 2 - 1, -(y / H) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, this.camera);
    const objs: THREE.Object3D[] = [];
    for (const l of this.layers) if (l.visible && l.status === "ready" && l.def.kind !== "plan") objs.push(...l.pickables());
    let best: THREE.Vector3 | null = null;
    let bestD = Infinity;
    try {
      for (const hit of ray.intersectObjects(objs, false)) {
        if (this.cut.on && hit.point.y > this.cut.z + 1e-3) continue;
        if (hit.distance < bestD) {
          bestD = hit.distance;
          best = hit.point.clone();
        }
        break;
      }
    } catch (e) {
      console.warn("raycast failed", e);
    }
    const floorPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -(this.scene.levels?.floor ?? 0));
    const fp = new THREE.Vector3();
    if (ray.ray.intersectPlane(floorPlane, fp)) {
      const d = fp.distanceTo(ray.ray.origin);
      if (d < bestD) best = fp;
    }
    return best;
  }

  flyToPoint(p: THREE.Vector3) {
    if (this.mode === "plan") {
      this.planCenter.set(p.x, 0, p.z);
      this.updateOrtho();
      this.changed();
      return;
    }
    if (this.walking) {
      const e = new THREE.Vector3(p.x, (this.scene.levels?.floor ?? 0) + EYE_HEIGHT, p.z);
      const off = this.controls.target.clone().sub(this.persp.position);
      this.flyTo(e, e.clone().add(off), 700);
      return;
    }
    const off = this.persp.position.clone().sub(this.controls.target);
    const dist = Math.max(1.5, off.length() * 0.5);
    this.flyTo(p.clone().add(off.normalize().multiplyScalar(dist)), p.clone());
  }

  // ------------------------------------------------------------------ input

  private bindInput() {
    const el = this.renderer.domElement;
    let down: { x: number; y: number } | null = null;
    let planDrag: { x: number; y: number; c: THREE.Vector3 } | null = null;
    el.addEventListener("pointerdown", (e) => {
      down = { x: e.clientX, y: e.clientY };
      if (this.mode === "plan") {
        planDrag = { x: e.clientX, y: e.clientY, c: this.planCenter.clone() };
        el.setPointerCapture(e.pointerId);
      }
    });
    el.addEventListener("pointermove", (e) => {
      if (planDrag) {
        const H = this.container.clientHeight;
        const mPerPx = (this.ortho.top - this.ortho.bottom) / H;
        this.planCenter.set(planDrag.c.x - (e.clientX - planDrag.x) * mPerPx, 0, planDrag.c.z - (e.clientY - planDrag.y) * mPerPx);
        this.updateOrtho();
        this.changed();
      } else if (!down) {
        const r = el.getBoundingClientRect();
        const hit = this.cams?.pick(this.camera, e.clientX - r.left, e.clientY - r.top, r.width, r.height);
        el.style.cursor = hit ? "pointer" : "";
      }
    });
    el.addEventListener("pointerup", (e) => {
      planDrag = null;
      if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) < 4) {
        const r = el.getBoundingClientRect();
        const hit = this.cams?.pick(this.camera, e.clientX - r.left, e.clientY - r.top, r.width, r.height);
        if (hit) this.enterPhoto(hit);
      }
      down = null;
    });
    el.addEventListener(
      "wheel",
      (e) => {
        if (this.mode !== "plan") return;
        e.preventDefault();
        this.planZoom = THREE.MathUtils.clamp(this.planZoom * Math.exp(-e.deltaY * 0.0015), 0.3, 12);
        this.updateOrtho();
        this.changed();
      },
      { passive: false },
    );
    el.addEventListener("dblclick", (e) => {
      if (this.photo) return;
      const r = el.getBoundingClientRect();
      const p = this.pickWorld(e.clientX - r.left, e.clientY - r.top);
      if (p) this.flyToPoint(p);
    });
    this.controls.addEventListener("change", () => this.changed());

    window.addEventListener("keydown", (e) => {
      if (isTextEntry(e.target)) return;
      const k = e.key.toLowerCase();
      if (this.photo) {
        if (k === "arrowright") this.stepPhoto(1);
        else if (k === "arrowleft") this.stepPhoto(-1);
        else if (k === "escape") this.exitPhoto();
        return;
      }
      if (k === "escape") {
        if (this.walking) this.exitWalk();
        return;
      }
      if (k === "r" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        if (this.walking) this.exitWalk();
        if (this.mode === "plan") {
          const b = this.scene.bounds;
          this.planCenter.copy(w2t((b.min[0] + b.max[0]) / 2, (b.min[1] + b.max[1]) / 2, 0));
          this.planZoom = 1;
          this.updateOrtho();
          this.changed();
        } else this.resetView();
        return;
      }
      if (["w", "a", "s", "d"].includes(k) && !e.metaKey && !e.ctrlKey) {
        if (!this.walking && this.mode === "3d") this.enterWalk();
      }
      this.keys.add(k);
      if (e.shiftKey) this.keys.add("shift");
    });
    window.addEventListener("keyup", (e) => {
      this.keys.delete(e.key.toLowerCase());
      if (!e.shiftKey) this.keys.delete("shift");
    });
    window.addEventListener("blur", () => this.keys.clear());
  }

  private resize() {
    const W = this.container.clientWidth;
    const H = this.container.clientHeight;
    if (!W || !H) return;
    this.renderer.setSize(W, H);
    this.persp.aspect = W / H;
    this.persp.updateProjectionMatrix();
    if (this.photo && !this.tweens.length) {
      this.persp.fov = letterboxFov(this.photo, W, H);
      this.applyPhotoProjection();
    }
    this.updateOrtho();
    this.changed();
  }

  // ------------------------------------------------------------------ loop

  private frame() {
    const dt = Math.min(this.clock.getDelta(), 0.1);
    const now = performance.now();
    if (this.tweens.length) {
      const tw = this.tweens[0];
      const k = Math.min(1, (now - tw.t0) / tw.dur);
      tw.step(ease(k));
      if (k >= 1) {
        this.tweens.shift();
        tw.done?.();
      }
      this.changed();
    }
    this.walkStep(dt);
    if (this.controls.enabled && !this.tweens.length) this.controls.update();
    const cam = this.camera;
    for (const l of this.layers) if (l.visible) l.update(dt, cam);
    this.renderer.render(this.scene3, cam);
    this.frames++;
    if (now - this.fpsT > 500) {
      this.stats.fps = (this.frames * 1000) / (now - this.fpsT);
      this.stats.calls = this.renderer.info.render.calls;
      this.stats.tris = this.renderer.info.render.triangles;
      this.frames = 0;
      this.fpsT = now;
      this.dispatchEvent(new Event("stats"));
    }
  }
}
