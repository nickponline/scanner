import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { Layer, type LayerCtx } from "./base";
import type { LayerDef } from "../types";

export async function loadGltf(ctx: LayerCtx, url: string) {
  const loader = new GLTFLoader();
  loader.setMeshoptDecoder(MeshoptDecoder);
  loader.setRequestHeader(ctx.fetcher.headersFor(url));
  return loader.loadAsync(url);
}

export interface CadPart {
  id: string;
  url: string;
}

/**
 * CAD: the Argon agent's building model, merged from the per-part GLBs (architecture, furniture, ceilings)
 * into one layer. Modes: solid | edges | ceilings (solid with translucent ceilings).
 */
export class CadLayer extends Layer {
  private meshes: THREE.Mesh[] = [];
  private ceilingMeshes: THREE.Object3D[] = [];
  // Unlit translucent neutral fill (Argon GLBs may lack normals, which rendered black under Lambert) + crisp edges.
  fillOpacity = 0.25;
  private solidMat = new THREE.MeshBasicMaterial({
    color: 0x9aa0a8,
    transparent: true,
    opacity: 0.3,
    side: THREE.DoubleSide,
    depthWrite: false,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
  });
  private furnitureMat = new THREE.MeshBasicMaterial({
    color: 0xc3c7cd,
    transparent: true,
    opacity: 0.35,
    side: THREE.DoubleSide,
    depthWrite: false,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
  });
  private glassMat = new THREE.MeshBasicMaterial({
    color: 0x7fd9cc,
    transparent: true,
    opacity: 0.4,
    side: THREE.DoubleSide,
    depthWrite: false,
  });
  private ceilingMat = new THREE.MeshBasicMaterial({
    color: 0x9aa0a8,
    transparent: true,
    opacity: 0.1,
    side: THREE.DoubleSide,
    depthWrite: false,
  });
  private edgeMat = new THREE.LineBasicMaterial({ color: 0xe6e8eb, transparent: true, opacity: 0.85, depthWrite: false });
  private windows = new Set<string>();
  private counts = { walls: 0, openings: 0, furniture: 0, rooms: 0, area: 0 };

  constructor(
    def: LayerDef,
    ctx: LayerCtx,
    readonly parts: CadPart[],
  ) {
    super(def, ctx);
    this.clipGroup = "model";
  }

  get modes() {
    return ["solid", "edges", "ceilings"];
  }

  protected async load() {
    const bm = await this.ctx.buildingModel;
    for (const st of bm?.storeys ?? []) {
      for (const o of st.openings ?? []) if (o.type === "window") this.windows.add(o.id);
      this.counts.walls += st.walls?.length ?? 0;
      this.counts.openings += st.openings?.length ?? 0;
      this.counts.furniture += st.furniture?.length ?? 0;
      this.counts.rooms += st.rooms?.length ?? 0;
      for (const r of st.rooms ?? []) this.counts.area += r.area_m2 ?? 0;
    }
    const loaded = await Promise.allSettled(this.parts.map((p) => loadGltf(this.ctx, this.ctx.resolve(p.url))));
    let ok = 0;
    loaded.forEach((res, i) => {
      if (res.status !== "fulfilled") {
        console.warn(`CAD part ${this.parts[i].id} failed:`, res.reason);
        return;
      }
      ok++;
      const part = this.parts[i].id;
      const root = res.value.scene;
      root.name = `cad:${part}`;
      this.object.add(root);
      if (part === "ceilings") this.ceilingMeshes.push(root);
      root.traverse((o) => {
        const mesh = o as THREE.Mesh;
        if (!mesh.isMesh) return;
        this.meshes.push(mesh);
        const elementId = nodeElementId(mesh);
        mesh.userData.elementId = elementId;
        mesh.userData.part = part;
        mesh.userData.glass = this.windows.has(elementId);
        if (part === "ceilings") return;
        const lines = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry, 25), this.edgeMat);
        lines.renderOrder = 5;
        lines.raycast = () => {};
        mesh.add(lines);
      });
    });
    if (!ok) throw new Error("no CAD parts loaded");
  }

  protected applyMode() {
    const edgesOnly = this.mode === "edges";
    for (const m of this.meshes) {
      const part = m.userData.part as string;
      m.material =
        part === "ceilings"
          ? this.ceilingMat
          : edgesOnly
            ? HIDDEN
            : m.userData.glass
              ? this.glassMat
              : part === "furniture"
                ? this.furnitureMat
                : this.solidMat;
    }
    for (const c of this.ceilingMeshes) c.visible = this.mode === "ceilings";
    this.edgeMat.color.set(0xe6e8eb);
    this.edgeMat.opacity = 0.9;
    this.setFillOpacity(this.fillOpacity, false);
    this.applyClip();
  }

  /** Fill transparency (0 = edges only look, 1 = opaque). Edges always stay visible on top. */
  setFillOpacity(x: number, notify = true) {
    this.fillOpacity = x;
    this.solidMat.opacity = x;
    this.furnitureMat.opacity = Math.min(1, x * 1.2);
    this.glassMat.opacity = x * 0.6;
    this.ceilingMat.opacity = x * 0.35;
    for (const m of [this.solidMat, this.furnitureMat, this.glassMat, this.ceilingMat]) {
      m.transparent = x < 0.999;
      m.depthWrite = x >= 0.999;
      m.needsUpdate = true;
    }
    if (notify) this.ctx.changed();
  }

  protected applyClip() {
    const planes = this.clipped ? [this.ctx.clipPlane] : null;
    for (const m of [this.solidMat, this.furnitureMat, this.glassMat, this.ceilingMat, this.edgeMat]) {
      m.clippingPlanes = planes;
      m.needsUpdate = true;
    }
  }

  summary() {
    if (this.status === "error") return "failed";
    const c = this.counts;
    if (!c.walls && !c.furniture) return this.def.summary ?? "Argon";
    const bits = [`${c.walls} walls`, `${c.openings} openings`, `${c.furniture} objects`];
    if (c.area) bits.unshift(`${c.area.toFixed(1)} m²`);
    return bits.join(" · ");
  }

  statusLine() {
    return `CAD · Argon building model · ${this.summary()} · ${this.mode}`;
  }

  pickables() {
    return this.meshes;
  }
}

/** Invisible material that still lets the mesh be raycast and its edge children render. */
const HIDDEN = new THREE.MeshBasicMaterial({ visible: false });

function nodeElementId(mesh: THREE.Object3D): string {
  // gltfpack may wrap meshes; the named node is the nearest named ancestor.
  let o: THREE.Object3D | null = mesh;
  while (o && !o.name) o = o.parent;
  return o?.name ?? "";
}

/** CoMe mesh: color | normals | wire. */
export class MeshLayer extends Layer {
  private meshes: THREE.Mesh[] = [];
  private colorMat = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide });
  private litMat = new THREE.MeshLambertMaterial({ color: 0xd8d8d8, side: THREE.DoubleSide });
  private normalMat = new THREE.MeshNormalMaterial({ side: THREE.DoubleSide });
  private wireMat = new THREE.MeshBasicMaterial({ color: 0xdfe3ea, wireframe: true, transparent: true, opacity: 0.35 });
  private tris = 0;

  constructor(def: LayerDef, ctx: LayerCtx) {
    super(def, ctx);
    this.clipGroup = "mesh";
  }

  protected async load() {
    const gltf = await loadGltf(this.ctx, this.ctx.resolve(this.def.url!));
    this.object.add(gltf.scene);
    gltf.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (!m.isMesh) return;
      this.meshes.push(m);
      const g = m.geometry;
      this.tris += (g.index ? g.index.count : g.attributes.position.count) / 3;
    });
  }

  protected applyMode() {
    for (const m of this.meshes) {
      const hasColor = !!m.geometry.attributes.color;
      m.material =
        this.mode === "normals" ? this.normalMat : this.mode === "wire" ? this.wireMat : hasColor ? this.colorMat : this.litMat;
    }
    this.applyClip();
  }

  protected applyClip() {
    const planes = this.clipped ? [this.ctx.clipPlane] : null;
    for (const m of [this.colorMat, this.litMat, this.normalMat, this.wireMat]) {
      m.clippingPlanes = planes;
      m.needsUpdate = true;
    }
  }

  statusLine() {
    return `Mesh · ${this.def.summary ?? ""}: ${Math.round(this.tris).toLocaleString("en-US")} triangles, ${this.mode} (${(this.loadSeconds ?? 0).toFixed(1)} s)`;
  }

  pickables() {
    return this.meshes;
  }
}
