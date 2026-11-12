import * as THREE from "three";
import type { SparkRenderer } from "@sparkjsdev/spark";
import type { Fetcher } from "../data";
import type { BuildingModel, LayerDef, SceneJson } from "../types";

export interface LayerCtx {
  root: THREE.Object3D;
  scene3: THREE.Scene;
  renderer: THREE.WebGLRenderer;
  spark: SparkRenderer;
  fetcher: Fetcher;
  scene: SceneJson;
  resolve(url: string): string;
  buildingModel: Promise<BuildingModel | null>;
  clipPlane: THREE.Plane;
  changed(): void;
}

export type ClipGroup = "splat" | "model" | "mesh" | "points";
export type LayerStatus = "loading" | "ready" | "error";

export abstract class Layer {
  readonly object = new THREE.Group();
  visible: boolean;
  mode: string | undefined;
  status: LayerStatus = "loading";
  error?: string;
  loadSeconds?: number;
  /** Which section-clip group this layer belongs to, if any. */
  clipGroup: ClipGroup | null = null;
  clipped = false;
  private started = false;
  private loadPromise?: Promise<void>;

  constructor(
    public def: LayerDef,
    public ctx: LayerCtx,
  ) {
    this.visible = def.visible !== false;
    this.mode = def.mode ?? def.modes?.[0];
    this.object.name = `layer:${def.id}`;
    this.object.visible = this.visible;
    ctx.root.add(this.object);
  }

  get id() {
    return this.def.id;
  }

  /** Modes offered in the UI (contract modes, or layer-specific synthetic ones). */
  get modes(): string[] {
    return this.def.modes ?? [];
  }

  /** Load lazily: hidden layers load on first show. */
  ensureLoaded(): Promise<void> {
    if (!this.started) {
      this.started = true;
      const t0 = performance.now();
      this.loadPromise = this.load()
        .then(() => {
          this.status = "ready";
          this.loadSeconds = (performance.now() - t0) / 1000;
          this.applyMode();
          this.applyClip();
        })
        .catch((e: unknown) => {
          this.status = "error";
          this.error = e instanceof Error ? e.message : String(e);
          console.warn(`layer ${this.id} failed:`, e);
        })
        .finally(() => this.ctx.changed());
    }
    return this.loadPromise!;
  }

  protected abstract load(): Promise<void>;

  setVisible(v: boolean) {
    this.visible = v;
    this.object.visible = v;
    if (v) void this.ensureLoaded();
    this.ctx.changed();
  }

  setMode(m: string) {
    this.mode = m;
    if (this.status === "ready") this.applyMode();
    this.ctx.changed();
  }

  setClipped(on: boolean) {
    this.clipped = on;
    if (this.status === "ready") this.applyClip();
  }

  protected applyMode() {}
  protected applyClip() {
    const planes = this.clipped ? [this.ctx.clipPlane] : null;
    this.object.traverse((o) => {
      const mats = (o as THREE.Mesh).material;
      if (!mats) return;
      for (const m of Array.isArray(mats) ? mats : [mats]) {
        if (m.clippingPlanes !== planes) {
          m.clippingPlanes = planes;
          m.needsUpdate = true;
        }
      }
    });
  }

  /** Summary text shown in the layers panel row. */
  summary(): string {
    if (this.status === "error") return `failed · ${this.def.summary ?? ""}`.trim();
    return this.def.summary ?? "";
  }

  /** Long stats line for the status bar. */
  statusLine(): string {
    return `${this.def.label}${this.def.summary ? ` · ${this.def.summary}` : ""}`;
  }

  update(_dt: number, _camera: THREE.Camera) {}

  /** Meshes to raycast for double-click fly-to. */
  pickables(): THREE.Object3D[] {
    return [];
  }
}

export function fmtInt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

export function fmtK(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} k`;
  return String(n);
}
