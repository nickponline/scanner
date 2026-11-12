// Deep links: ?v=&layers=&cam=&cut=&mode=  (REQUIREMENTS §6.3)
//   layers = comma list of visible layer ids, each optionally ":mode" (e.g. architecture:solid,plan:technical)
//   cam    = "px,py,pz,tx,ty,tz" in the World frame (Z up), or "photo:<camera id>"
//   cut    = section height in metres (cut on) or "off"
//   mode   = "plan" for the top-down view (default 3D)

import type { Viewer } from "./viewer";

export interface DeepLink {
  layers?: { id: string; mode?: string }[];
  cam?: number[];
  photo?: string;
  cut?: number | "off";
  mode?: "3d" | "plan";
}

export function readDeepLink(search: string = window.location.search): DeepLink {
  const q = new URLSearchParams(search);
  const out: DeepLink = {};
  const layers = q.get("layers");
  if (layers != null) {
    out.layers = layers
      .split(",")
      .filter(Boolean)
      .map((s) => {
        const [id, mode] = s.split(":");
        return { id, mode: mode || undefined };
      });
  }
  const cam = q.get("cam");
  if (cam) {
    if (cam.startsWith("photo:")) out.photo = cam.slice(6);
    else {
      const n = cam.split(",").map(Number);
      if (n.length === 6 && n.every(Number.isFinite)) out.cam = n;
    }
  }
  const cut = q.get("cut");
  if (cut) out.cut = cut === "off" ? "off" : Number.isFinite(Number(cut)) ? Number(cut) : undefined;
  const mode = q.get("mode");
  if (mode === "plan" || mode === "3d") out.mode = mode;
  return out;
}

export async function applyDeepLink(v: Viewer, d: DeepLink) {
  if (d.layers) {
    const want = new Map(d.layers.map((l) => [l.id, l.mode]));
    for (const l of v.layers) {
      const on = want.has(l.id);
      const m = want.get(l.id);
      if (m && (l.modes.includes(m) || !l.modes.length)) l.setMode(m);
      if (l.visible !== on) l.setVisible(on);
    }
    if (v.activeLayerId && !want.has(v.activeLayerId)) {
      const first = v.layers.find((l) => l.visible);
      if (first) v.setActiveLayer(first.id);
    }
  }
  if (typeof d.cut === "number") {
    v.setCutZ(d.cut);
    v.setCutOn(true);
  } else if (d.cut === "off") v.setCutOn(false);
  if (d.mode) v.setMode(d.mode);
  if (d.cam) v.setView(d.cam.slice(0, 3), d.cam.slice(3, 6));
  if (d.photo) {
    const cams = v.cams;
    if (cams) {
      await cams.ensureLoaded();
      const c = cams.cameras.find((x) => x.id === d.photo);
      if (c) v.enterPhoto(c);
    }
  }
}

const r2 = (x: number) => Math.round(x * 100) / 100;

export function buildDeepLink(v: Viewer, base: string = window.location.href): string {
  const url = new URL(base);
  const q = url.searchParams;
  q.set("layers", v.layers.filter((l) => l.visible).map((l) => (l.mode && l.modes.length > 1 ? `${l.id}:${l.mode}` : l.id)).join(","));
  if (v.photo) q.set("cam", `photo:${v.photo.id}`);
  else {
    const vs = v.viewState();
    q.set("cam", [...vs.pos, ...vs.target].map(r2).join(","));
  }
  q.set("cut", v.cut.on ? v.cut.z.toFixed(2) : "off");
  if (v.mode === "plan") q.set("mode", "plan");
  else q.delete("mode");
  if (v.scene.version != null && !q.has("scene")) q.set("v", String(v.scene.version).padStart(2, "0"));
  return url.toString();
}

/** Keep the address bar in sync (debounced, replaceState). */
export function syncDeepLink(v: Viewer) {
  let timer = 0;
  v.addEventListener("change", () => {
    clearTimeout(timer);
    timer = window.setTimeout(() => {
      const next = buildDeepLink(v);
      if (next !== window.location.href) history.replaceState(null, "", next);
    }, 400);
  });
}
