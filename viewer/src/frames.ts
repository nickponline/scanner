// Coordinate frames (CONTRACTS.md §1).
// World: right-handed, Z up, metres. three.js: Y up. Everything lives under one root Object3D rotated
// −90° about X, so a World point (x, y, z) appears at three (x, z, −y).

import * as THREE from "three";
import type { CameraDef } from "./types";

export const ROOT_MATRIX = new THREE.Matrix4().makeRotationX(-Math.PI / 2);

export function w2t(x: number, y: number, z: number): THREE.Vector3 {
  return new THREE.Vector3(x, z, -y);
}

export function t2w(v: THREE.Vector3): [number, number, number] {
  return [v.x, -v.z, v.y];
}

const FLIP_CV_TO_GL = new THREE.Matrix4().makeScale(1, -1, -1);

/** OpenCV c2w (row-major, World frame) → three camera position/quaternion (three world frame). */
export function poseFromC2W(c2w: number[]): { position: THREE.Vector3; quaternion: THREE.Quaternion } {
  const m = new THREE.Matrix4();
  // Matrix4.set takes row-major arguments.
  m.set(
    c2w[0], c2w[1], c2w[2], c2w[3],
    c2w[4], c2w[5], c2w[6], c2w[7],
    c2w[8], c2w[9], c2w[10], c2w[11],
    c2w[12] ?? 0, c2w[13] ?? 0, c2w[14] ?? 0, c2w[15] ?? 1,
  );
  const t = ROOT_MATRIX.clone().multiply(m).multiply(FLIP_CV_TO_GL);
  const position = new THREE.Vector3();
  const quaternion = new THREE.Quaternion();
  const scale = new THREE.Vector3();
  t.decompose(position, quaternion, scale);
  return { position, quaternion };
}

/** World-frame camera centre of a cameras.json entry. */
export function camCenterWorld(c: CameraDef): [number, number, number] {
  return [c.c2w[3], c.c2w[7], c.c2w[11]];
}

/** World-frame forward (OpenCV +Z) of a cameras.json entry. */
export function camForwardWorld(c: CameraDef): [number, number, number] {
  return [c.c2w[2], c.c2w[6], c.c2w[10]];
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Letterbox an image of size iw×ih inside a W×H viewport. */
export function fitRect(iw: number, ih: number, W: number, H: number): Rect {
  const s = Math.min(W / iw, H / ih);
  const w = iw * s;
  const h = ih * s;
  return { x: (W - w) / 2, y: (H - h) / 2, w, h };
}

/**
 * Off-axis projection reproducing pinhole intrinsics inside a letterboxed rect of the W×H canvas.
 * The image (iw×ih, fx, fy, cx, cy) is drawn at `rect`; the projection maps the same rays to the same pixels.
 */
export function intrinsicsProjection(
  cam: CameraDef,
  W: number,
  H: number,
  near: number,
  far: number,
): THREE.Matrix4 {
  const r = fitRect(cam.w, cam.h, W, H);
  const s = r.w / cam.w;
  const fx = cam.fx * s;
  const fy = cam.fy * s;
  const cx = cam.cx * s + r.x;
  const cy = cam.cy * s + r.y;
  const left = (-cx / fx) * near;
  const right = ((W - cx) / fx) * near;
  const top = (cy / fy) * near;
  const bottom = (-(H - cy) / fy) * near;
  return new THREE.Matrix4().makePerspective(left, right, top, bottom, near, far);
}

/** Vertical FOV in degrees for intrinsics when displayed letterboxed in a W×H canvas. */
export function letterboxFov(cam: CameraDef, W: number, H: number): number {
  const r = fitRect(cam.w, cam.h, W, H);
  const fy = cam.fy * (r.w / cam.w);
  return THREE.MathUtils.radToDeg(2 * Math.atan(H / 2 / fy));
}
