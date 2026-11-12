import * as THREE from "three";
import {
  SplatEdit,
  SplatEditRgbaBlendMode,
  SplatEditSdf,
  SplatEditSdfType,
  SplatFileType,
  SplatMesh,
} from "@sparkjsdev/spark";
import { Layer, fmtInt, fmtK, type LayerCtx } from "./base";
import type { LayerDef } from "../types";

function extOf(url: string): string {
  const path = url.split(/[?#]/, 1)[0];
  const m = path.match(/\.([a-z0-9]+)$/i);
  return m ? m[1].toLowerCase() : "";
}

/** Gaussian splat (.rad streamed LOD, or .spz / .ply / .splat / .sog). */
export class SplatLayer extends Layer {
  mesh?: SplatMesh;
  loadedUrl?: string;
  /** Horizontal section: a plane SDF that zeroes opacity above the cut. Global edit (child of root). */
  private edit: SplatEdit;
  private sdf: SplatEditSdf;
  private shDegree = 3;

  constructor(def: LayerDef, ctx: LayerCtx) {
    super(def, ctx);
    this.clipGroup = "splat";
    this.sdf = new SplatEditSdf({ type: SplatEditSdfType.PLANE, invert: true, opacity: 0 });
    this.edit = new SplatEdit({ rgbaBlendMode: SplatEditRgbaBlendMode.MULTIPLY, sdfs: [this.sdf] });
    this.edit.name = "splat-section";
    this.edit.add(this.sdf);
    this.edit.visible = false;
    this.object.add(this.edit);
  }

  protected async load() {
    const urls = [this.def.url, this.def.fallback_url].filter(Boolean).map((u) => this.ctx.resolve(u!));
    let lastErr: unknown = null;
    for (const url of urls) {
      try {
        await this.loadUrl(url);
        this.loadedUrl = url;
        return;
      } catch (e) {
        console.warn(`splat: failed to load ${url}`, e);
        lastErr = e;
      }
    }
    throw lastErr ?? new Error("no splat url");
  }

  private async loadUrl(url: string) {
    const ext = extOf(url);
    const isRad = ext === "rad";
    const mesh = new SplatMesh({
      url,
      requestHeader: this.ctx.fetcher.headersFor(url),
      ...(isRad ? { paged: true, fileType: SplatFileType.RAD } : {}),
    });
    mesh.name = "splat";
    this.object.add(mesh);
    try {
      await mesh.initialized;
    } catch (e) {
      this.object.remove(mesh);
      mesh.dispose();
      throw e;
    }
    mesh.maxSh = this.shDegree;
    this.mesh = mesh;
  }

  /** Set the section height (World z). */
  setCut(z: number) {
    this.sdf.position.set(0, 0, z);
  }

  /** Current section height applied to the splat (World z). */
  get cutZ(): number {
    return this.sdf.position.z;
  }

  protected applyClip() {
    this.edit.visible = this.clipped;
  }

  setShDegree(n: number) {
    this.shDegree = n;
    if (this.mesh) this.mesh.maxSh = n;
  }

  get count(): number {
    const s = this.def.stats?.count;
    if (s) return s;
    const m = this.mesh as unknown as { numSplats?: number; packedSplats?: { numSplats?: number } } | undefined;
    return m?.packedSplats?.numSplats ?? m?.numSplats ?? 0;
  }

  statusLine() {
    const st = this.def.stats ?? {};
    const parts = [`${fmtInt(this.count)} splats`];
    if (st.sh_degree != null) parts.push(`SH ${st.sh_degree}`);
    if (st.lod_levels) parts.push(`LOD ${st.lod_levels} levels / ${fmtInt(st.lod_nodes ?? 0)} nodes`);
    const fmt = this.loadedUrl ? extOf(this.loadedUrl).toUpperCase() : "";
    if (fmt) parts.push(fmt === "RAD" ? "RAD streamed" : fmt);
    return `Gaussian splat · ${this.def.summary ?? ""}: ${parts.join(", ")} (${(this.loadSeconds ?? 0).toFixed(1)} s)`;
  }

  summary() {
    const base = super.summary() || `${fmtK(this.count)} splats`;
    return base;
  }

  pickables(): THREE.Object3D[] {
    return this.mesh ? [this.mesh] : [];
  }
}
