import * as THREE from "three";
import { PLYLoader } from "three/examples/jsm/loaders/PLYLoader.js";
import { Layer, fmtInt, type LayerCtx } from "./base";
import type { LayerDef } from "../types";

const VERT = /* glsl */ `
#include <common>
#include <clipping_planes_pars_vertex>
uniform float uSize;
uniform float uCull;
uniform float uPixelRatio;
varying vec3 vColor;
void main() {
  #ifdef USE_COLOR
  vColor = color;
  #else
  vColor = vec3(0.85);
  #endif
  vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
  gl_Position = projectionMatrix * mvPosition;
  gl_PointSize = uSize * uPixelRatio;
  #ifdef HAS_NORMAL
  vec3 n = normalize(normalMatrix * normal);
  if (uCull > 0.5 && dot(n, -mvPosition.xyz) < 0.0) gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
  #endif
  #include <clipping_planes_vertex>
}`;

const FRAG = /* glsl */ `
#include <common>
#include <clipping_planes_pars_fragment>
varying vec3 vColor;
void main() {
  #include <clipping_planes_fragment>
  vec2 c = gl_PointCoord - 0.5;
  if (dot(c, c) > 0.25) discard;
  gl_FragColor = vec4(vColor, 1.0);
  #include <colorspace_fragment>
}`;

export class PointsLayer extends Layer {
  private mat: THREE.ShaderMaterial;
  private count = 0;

  constructor(def: LayerDef, ctx: LayerCtx) {
    super(def, ctx);
    this.clipGroup = "points";
    this.mode = def.mode ?? "culled";
    this.mat = new THREE.ShaderMaterial({
      vertexShader: VERT,
      fragmentShader: FRAG,
      uniforms: {
        uSize: { value: 2.0 },
        uCull: { value: 1 },
        uPixelRatio: { value: Math.min(window.devicePixelRatio, 2) },
      },
      clipping: true,
    });
  }

  get modes() {
    return this.def.modes ?? ["culled", "all"];
  }

  setPointSize(px: number) {
    this.mat.uniforms.uSize.value = px;
  }

  get pointSize(): number {
    return this.mat.uniforms.uSize.value;
  }

  protected async load() {
    const url = this.ctx.resolve(this.def.url!);
    const loader = new PLYLoader();
    loader.setRequestHeader(this.ctx.fetcher.headersFor(url));
    const geom = await loader.loadAsync(url);
    this.count = geom.attributes.position.count;
    this.mat.vertexColors = !!geom.attributes.color;
    if (geom.attributes.normal) this.mat.defines = { HAS_NORMAL: 1 };
    this.mat.needsUpdate = true;
    const pts = new THREE.Points(geom, this.mat);
    pts.frustumCulled = true;
    this.object.add(pts);
  }

  protected applyMode() {
    this.mat.uniforms.uCull.value = this.mode === "all" ? 0 : 1;
  }

  protected applyClip() {
    this.mat.clippingPlanes = this.clipped ? [this.ctx.clipPlane] : null;
    this.mat.needsUpdate = true;
  }

  statusLine() {
    return `Point cloud · ${fmtInt(this.count)} points, ${this.mode === "all" ? "all faces" : "back faces hidden"}, ${this.pointSize} px (${(this.loadSeconds ?? 0).toFixed(1)} s)`;
  }
}
