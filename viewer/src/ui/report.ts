// Per-stage processing report (GET /v1/scans/{id}/report): rendered inline under a scan row on the index page
// and as a full page at /scans/{id}/report.

import { Fetcher } from "../data";
import { h, s } from "./dom";

type Dict = Record<string, unknown>;

export interface StageReport {
  name: string;
  version: number | null;
  reused: boolean;
  status: string | null;
  started_at?: string;
  ended_at?: string;
  duration_s?: number;
  progress?: number;
  note?: string;
  metrics?: Dict | null;
  error?: string;
  job?: { config?: Dict; image?: string; git_sha?: string; duration_s?: number; finished_at?: string };
  files: { path: string; files: number; bytes: number }[];
  bytes: number;
  logs: { name: string; bytes: number; tail: string | null }[];
  images: { name: string; url?: string }[];
  data: Dict;
}

export interface Report {
  scan_id: string;
  name?: string;
  version: number;
  status: string;
  versions: number[];
  processing_version?: number;
  current_version?: number;
  created_at?: string;
  qa?: Dict;
  upload?: Dict;
  stages: StageReport[];
  generated_at: string;
}

const LABEL: Record<string, string> = {
  ingest: "Ingest", frames: "Frames", sfm: "Structure from motion", depth: "Depth & TSDF", splat: "Gaussian splat",
  densify: "Densify (DA3)", lod: "LOD packaging", mesh: "Mesh", plan: "Floor plan / CAD", publish: "Publish",
};
const SHORT: Record<string, string> = {
  ingest: "Ingest", frames: "Frames", sfm: "SfM", depth: "Depth", densify: "Densify", splat: "Splat", lod: "LOD", mesh: "Mesh", plan: "CAD", publish: "Publish",
};
const BLURB: Record<string, string> = {
  ingest: "Validates the upload: video, ARKit poses, LiDAR depth.",
  frames: "Picks keyframes from the video by camera motion, keeping the sharpest frame near each trigger.",
  sfm: "COLMAP feature extraction + matching + mapping, aligned to ARKit; unregistered frames get corrected ARKit poses.",
  depth: "Fuses LiDAR depth (consensus-filtered) into a TSDF; levels floor/ceiling and Manhattan-aligns the world.",
  splat: "gsplat MCMC training on the keyframes; every 8th keyframe held out for PSNR/SSIM.",
  lod: "Cleans floaters (bounds, size, opacity, needles) and builds Spark LOD (.rad) + .spz.",
  densify: "Depth Anything 3, conditioned on the SfM poses, predicts depth for every view; each map is checked against LiDAR, filtered by confidence and multi-view consistency, then voxel-fused into a dense point cloud (also used to initialise the splat).",
  mesh: "CoMe surface reconstruction from the splat + depth (retired).",
  plan: "Gemini agent drafts walls, openings and furniture from renders and photos.",
  publish: "Writes the viewer bundle: scene.json, photos, layers.",
};
// dark-mode reference palette (validated against the panel surface): series 1 blue, series 2 orange
// single-accent palette: series 1 teal, series 2 neutral grey (always paired with a legend)
const C1 = "#2ec4b0";
const C2 = "#7d828b";

// ---------------------------------------------------------------- formatting
export function fmtBytes(b?: number | null): string {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = b;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`;
}

export function fmtDur(sec?: number | null): string {
  if (sec == null) return "–";
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)} s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} min ${Math.round(sec % 60)} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

function fmtTime(iso?: string): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtVal(v: unknown): string {
  if (v == null) return "–";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("en-GB") : String(Math.round(v * 10000) / 10000);
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v;
  const j = JSON.stringify(v);
  return j.length > 400 ? `${j.slice(0, 400)}…` : j;
}

function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function get(o: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((a, k) => (a && typeof a === "object" ? (a as Dict)[k] : undefined), o);
}

// ---------------------------------------------------------------- tooltip (one per report root)
class Tip {
  el = h("div", { class: "rp-tip", role: "tooltip" });
  show(ev: MouseEvent | FocusEvent, rows: [string, string][]) {
    this.el.replaceChildren(
      ...rows.map(([k, v]) => h("div", { class: "rp-tip-row" }, h("b", null, v), h("span", null, k))),
    );
    this.el.style.display = "block";
    const host = this.el.offsetParent as HTMLElement | null;
    const hr = host?.getBoundingClientRect() ?? { left: 0, top: 0 };
    let x: number;
    let y: number;
    if (ev instanceof MouseEvent) {
      x = ev.clientX;
      y = ev.clientY;
    } else {
      const r = (ev.target as Element).getBoundingClientRect();
      x = r.left + r.width / 2;
      y = r.top;
    }
    this.el.style.left = `${x - hr.left + 12}px`;
    this.el.style.top = `${y - hr.top - 12 - this.el.offsetHeight}px`;
  }
  hide() {
    this.el.style.display = "none";
  }
}

// ---------------------------------------------------------------- charts (SVG, one y-axis each)
interface Bar {
  label: string;
  value: number;
  series?: 0 | 1;
  extra?: [string, string][];
}

function axisTicks(max: number, min = 0, n = 4): number[] {
  const span = max - min || 1;
  const raw = span / n;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((s_) => s_ >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(Math.round(v * 1e6) / 1e6);
  return out;
}

function barChart(
  tip: Tip,
  bars: Bar[],
  o: { unit?: string; ref?: { value: number; label: string }; height?: number; yMin?: number; yMax?: number; legend?: [string, string] },
): HTMLElement {
  const W = 720;
  const H = o.height ?? 150;
  const L = 44;
  const R = 8;
  const T = 10;
  const B = 18;
  const vmax = o.yMax ?? (Math.max(...bars.map((b) => b.value), o.ref?.value ?? 0) * 1.08 || 1);
  const vmin = o.yMin ?? 0;
  const y = (v: number) => T + (H - T - B) * (1 - (v - vmin) / (vmax - vmin));
  const bw = (W - L - R) / Math.max(1, bars.length);
  const gap = bw > 6 ? 2 : bw > 3 ? 1 : 0;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "rp-chart", role: "img" });
  for (const t of axisTicks(vmax, vmin)) {
    svg.append(
      s("line", { x1: L, x2: W - R, y1: y(t), y2: y(t), class: "rp-grid" }),
      s("text", { x: L - 6, y: y(t) + 3, class: "rp-axis", "text-anchor": "end" }, fmtVal(t)),
    );
  }
  bars.forEach((b, i) => {
    const x = L + i * bw + gap / 2;
    const top = y(Math.min(vmax, Math.max(vmin, b.value)));
    const hgt = Math.max(1, y(vmin) - top);
    const r = Math.min(2, (bw - gap) / 2);
    const rect = s("rect", {
      x,
      y: top,
      width: Math.max(0.6, bw - gap),
      height: hgt,
      rx: r,
      fill: b.series === 1 ? C2 : C1,
      class: "rp-bar",
    });
    // hit target: full column height, wider than the mark
    const hit = s("rect", { x: L + i * bw, y: T, width: Math.max(bw, 3), height: H - T - B, fill: "transparent", tabindex: "-1" });
    const rows: [string, string][] = [[b.label, `${fmtVal(b.value)}${o.unit ?? ""}`], ...(b.extra ?? [])];
    hit.addEventListener("pointermove", (e) => {
      rect.classList.add("hot");
      tip.show(e as MouseEvent, rows);
    });
    hit.addEventListener("pointerleave", () => {
      rect.classList.remove("hot");
      tip.hide();
    });
    svg.append(rect, hit);
  });
  if (o.ref) {
    svg.append(
      s("line", { x1: L, x2: W - R, y1: y(o.ref.value), y2: y(o.ref.value), class: "rp-ref" }),
      s("text", { x: L + 4, y: y(o.ref.value) - 4, class: "rp-axis rp-reflbl" }, o.ref.label),
    );
  }
  svg.append(s("line", { x1: L, x2: W - R, y1: y(vmin), y2: y(vmin), class: "rp-base" }));
  const wrap = h("div", { class: "rp-chart-wrap" }, svg);
  if (o.legend)
    wrap.prepend(
      h(
        "div",
        { class: "rp-legend" },
        h("span", null, h("i", { style: { background: C1 } }), o.legend[0]),
        h("span", null, h("i", { style: { background: C2 } }), o.legend[1]),
      ),
    );
  return wrap;
}

function logTicks(lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(lo); e <= Math.ceil(hi); e++)
    for (const m of [1, 2, 5]) {
      const v = Math.log10(m * 10 ** e);
      if (v >= lo && v <= hi) out.push(v);
    }
  return out.length > 6 ? out.filter((v) => Math.abs(v - Math.round(v)) < 1e-9) : out;
}

function lineChart(tip: Tip, raw: { x: number; y: number }[], o: { xLabel: string; yLabel: string; height?: number; log?: boolean }): HTMLElement {
  const pts = raw.filter((p) => Number.isFinite(p.y) && (!o.log || p.y > 0));
  if (pts.length < 2) return h("div");
  const W = 480;
  const H = o.height ?? 170;
  const L = 48;
  const R = 10;
  const T = 10;
  const B = 22;
  const ys = pts.map((p) => (o.log ? Math.log10(Math.max(p.y, 1e-6)) : p.y));
  const xmax = Math.max(...pts.map((p) => p.x)) || 1;
  const xmin = Math.min(...pts.map((p) => p.x));
  const ymax = Math.max(...ys);
  const ymin = Math.min(...ys, o.log ? ymax : 0);
  const pad = (ymax - ymin) * 0.08 || 0.1;
  const y0 = o.log ? ymin - pad : 0;
  const y1 = ymax + pad;
  const X = (x: number) => L + ((W - L - R) * (x - xmin)) / (xmax - xmin || 1);
  const Y = (v: number) => T + (H - T - B) * (1 - (v - y0) / (y1 - y0 || 1));
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "rp-chart", role: "img" });
  for (const t of o.log ? logTicks(y0, y1) : axisTicks(y1, y0)) {
    const lbl = o.log ? fmtVal(Number((10 ** t).toPrecision(2))) : t >= 10000 ? `${t / 1000}k` : fmtVal(t);
    svg.append(
      s("line", { x1: L, x2: W - R, y1: Y(t), y2: Y(t), class: "rp-grid" }),
      s("text", { x: L - 6, y: Y(t) + 3, class: "rp-axis", "text-anchor": "end" }, lbl),
    );
  }
  for (const t of axisTicks(xmax, xmin, 4))
    svg.append(s("text", { x: X(t), y: H - 6, class: "rp-axis", "text-anchor": "middle" }, t >= 1000 ? `${t / 1000}k` : fmtVal(t)));
  const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join("");
  svg.append(s("path", { d, fill: "none", stroke: C1, "stroke-width": 2, "stroke-linejoin": "round" }));
  const hair = s("line", { y1: T, y2: H - B, class: "rp-hair" });
  const dot = s("circle", { r: 4, fill: C1, stroke: "var(--panel-solid)", "stroke-width": 2 });
  hair.style.display = dot.style.display = "none";
  const hit = s("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
  hit.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    let bi = 0;
    for (let i = 1; i < pts.length; i++) if (Math.abs(X(pts[i].x) - px) < Math.abs(X(pts[bi].x) - px)) bi = i;
    const p = pts[bi];
    hair.setAttribute("x1", String(X(p.x)));
    hair.setAttribute("x2", String(X(p.x)));
    dot.setAttribute("cx", String(X(p.x)));
    dot.setAttribute("cy", String(Y(ys[bi])));
    hair.style.display = dot.style.display = "";
    tip.show(e, [[o.yLabel, fmtVal(p.y)], [o.xLabel, fmtVal(p.x)]]);
  });
  hit.addEventListener("pointerleave", () => {
    hair.style.display = dot.style.display = "none";
    tip.hide();
  });
  svg.append(hair, dot, hit);
  return h("div", { class: "rp-chart-wrap" }, h("div", { class: "rp-chart-title" }, `${o.yLabel}${o.log ? " (log scale)" : ""} vs ${o.xLabel}`), svg);
}

// ---------------------------------------------------------------- pieces
function kv(rows: [string, unknown][]): HTMLElement {
  return h(
    "div",
    { class: "rp-kv" },
    ...rows.filter(([, v]) => v !== undefined).map(([k, v]) => h("div", { class: "rp-kv-item" }, h("span", { class: "k" }, k), h("span", { class: "v" }, fmtVal(v)))),
  );
}

function flatten(o: unknown, pre = "", out: [string, unknown][] = []): [string, unknown][] {
  if (o && typeof o === "object" && !Array.isArray(o)) {
    for (const [k, v] of Object.entries(o as Dict)) {
      if (v && typeof v === "object" && !Array.isArray(v) && Object.keys(v as Dict).length <= 12) flatten(v, `${pre}${k}.`, out);
      else out.push([`${pre}${k}`, v]);
    }
  } else out.push([pre.replace(/\.$/, ""), o]);
  return out;
}

function table(head: string[], rows: unknown[][], cls = ""): HTMLElement {
  return h(
    "div",
    { class: "rp-table-wrap" },
    h(
      "table",
      { class: `rp-table ${cls}` },
      h("thead", null, h("tr", null, ...head.map((c) => h("th", null, c)))),
      h("tbody", null, ...rows.map((r) => h("tr", null, ...r.map((c) => h("td", null, c instanceof Node ? c : fmtVal(c)))))),
    ),
  );
}

function details(title: string, body: Node, open = false): HTMLElement {
  const d = h("details", { class: "rp-details" }, h("summary", null, title), body);
  if (open) d.setAttribute("open", "");
  return d;
}

function section(title: string, ...body: (Node | null | false)[]): HTMLElement {
  return h("div", { class: "rp-sec" }, h("div", { class: "rp-sec-title" }, title), ...body);
}

function images(list: { name: string; url?: string }[], cls = ""): HTMLElement {
  return h(
    "div",
    { class: `rp-imgs ${cls}` },
    ...list
      .filter((i) => i.url)
      .map((i) =>
        h(
          "a",
          { href: i.url, target: "_blank", rel: "noopener", class: "rp-img", title: i.name },
          h("img", { src: i.url, alt: i.name, loading: "lazy" }),
          h("span", null, i.name.replace(/^.*\//, "")),
        ),
      ),
  );
}

function statusPill(st: string | null, reused: boolean, ver: number | null): HTMLElement {
  const s_ = st ?? "pending";
  const txt = { done: "Done", processing: "Running", running: "Running", failed: "Failed", pending: "Pending" }[s_] ?? s_;
  return h(
    "span",
    { class: "rp-pills" },
    h("span", { class: `rp-pill ${s_}` }, h("span", { class: "dot" }), txt),
    ver ? h("span", { class: "rp-pill ver" }, reused ? `reused v${ver}` : `v${ver}`) : null,
  );
}

// ---------------------------------------------------------------- stage-specific blocks
function stageHighlights(r: StageReport, tip: Tip): (Node | null)[] {
  const m = (r.metrics ?? {}) as Dict;
  const d = r.data ?? {};
  const out: (Node | null)[] = [];
  switch (r.name) {
    case "ingest": {
      const qa = (d.qa ?? {}) as Dict;
      out.push(
        kv([
          ["Video frames", m.frames],
          ["Duration", fmtDur(num(m.duration_s))],
          ["Path walked", m.path_m != null ? `${m.path_m} m` : undefined],
          ["LiDAR depth records", m.depth_records],
          ["Tracking limited", m.tracking_limited_frac != null ? `${((m.tracking_limited_frac as number) * 100).toFixed(1)} %` : undefined],
          ["Device", get(qa, "device.model")],
          ["iOS", get(qa, "device.os")],
          ["App", get(qa, "device.app_version")],
          ["Video", qa.video_format ? `${get(qa, "video_format.w")}×${get(qa, "video_format.h")} ${get(qa, "video_format.codec")} @ ${get(qa, "video_format.fps")} fps` : undefined],
          ["Segments", Array.isArray(qa.segments) ? qa.segments.length : undefined],
          ["Warnings", Array.isArray(qa.warnings) ? (qa.warnings.length ? qa.warnings.join(", ") : "none") : undefined],
        ]),
      );
      break;
    }
    case "frames": {
      out.push(
        kv([
          ["Keyframes", m.keyframes],
          ["With LiDAR depth", m.with_depth],
          ["Hero stills", m.hero],
          ["Keyframe spacing", m.kf_dist_m != null ? `${m.kf_dist_m} m / ${m.kf_rot_deg}°` : undefined],
        ]),
      );
      const fr = (d.frames ?? []) as { name: string; t: number; sharp: number; depth: boolean }[];
      if (fr.length) {
        const sh = fr.map((f) => f.sharp).sort((a, b) => a - b);
        const med = sh[Math.floor(sh.length / 2)];
        const p95 = sh[Math.floor(sh.length * 0.95)];
        const clipped = sh.filter((v) => v > p95 * 1.4).length;
        out.push(
          section(
            `Keyframe sharpness (Laplacian variance, higher = sharper) · median ${fmtVal(med)} · min ${fmtVal(sh[0])} · max ${fmtVal(sh[sh.length - 1])}${clipped ? ` · ${clipped} bars clipped at top, hover for value` : ""}`,
            barChart(
              tip,
              fr.map((f) => ({
                label: f.name,
                value: f.sharp,
                series: f.depth ? 0 : 1,
                extra: [["time", `${f.t.toFixed(1)} s`], ["LiDAR", f.depth ? "yes" : "no"]] as [string, string][],
              })),
              { legend: ["with LiDAR depth", "no depth"], ref: { value: med, label: "median" }, yMax: p95 * 1.4 },
            ),
          ),
        );
      }
      break;
    }
    case "sfm": {
      const reg = num(m.colmap_registered);
      const n = num(m.images);
      out.push(
        kv([
          ["Features / matcher", m.features ? `${m.features} / ${m.matcher}` : undefined],
          ["Mapper used", m.mapper],
          ["COLMAP registered", reg != null && n ? `${reg} / ${n} (${Math.round((reg / n) * 100)} %)` : undefined],
          ["Filled from ARKit", m.filled_from_arkit],
          ["ARKit residual p95", m.arkit_residual_p95_m != null ? `${((m.arkit_residual_p95_m as number) * 100).toFixed(1)} cm` : undefined],
          ["ARKit residual RMS", m.arkit_residual_rms_m != null ? `${((m.arkit_residual_rms_m as number) * 100).toFixed(1)} cm` : undefined],
          ["Gate p95", m.gate_p95_m != null ? `${((m.gate_p95_m as number) * 100).toFixed(1)} cm` : undefined],
          ["3D points", m.points3d],
          ["Mean track length", m.mean_track_len],
          ["Mean reprojection", m.mean_reproj_px != null ? `${m.mean_reproj_px} px` : undefined],
          ["Verified pairs", m.verified_pairs],
          ["Inlier matches", m.inlier_matches],
          ["ARKit path", m.arkit_path_m != null ? `${m.arkit_path_m} m` : undefined],
          ["Match cache", m.match_cache],
          ["Failed feature types", Object.keys(m).filter((k) => k.endsWith("_failed")).map((k) => k.replace("_failed", "")).join(", ") || "none"],
        ]),
      );
      const att = (m.attempts ?? {}) as Record<string, Dict>;
      if (Object.keys(att).length)
        out.push(
          section(
            "Mapper attempts",
            table(
              ["Mapper", "Passed gate", "Registered", "p95", "RMS", "3D points", "Outliers dropped", "Note"],
              Object.entries(att).map(([k, a]) => [
                k,
                a.ok ? "yes" : "no",
                a.registered,
                a.p95_m != null ? `${((a.p95_m as number) * 100).toFixed(1)} cm` : "–",
                a.rms_m != null ? `${((a.rms_m as number) * 100).toFixed(1)} cm` : "–",
                a.points3d,
                a.outliers_dropped,
                a.why ?? "",
              ]),
            ),
          ),
        );
      const res = (d.residuals ?? []) as [string, number][];
      const gate = num(m.gate_p95_m);
      if (res.length)
        out.push(
          section(
            "Per-frame camera position vs ARKit after alignment (cm), in capture order",
            barChart(
              tip,
              res.map(([nm, v]) => ({ label: nm, value: v * 100 })),
              { unit: " cm", ref: gate ? { value: gate * 100, label: `gate ${(gate * 100).toFixed(1)} cm` } : undefined },
            ),
          ),
        );
      break;
    }
    case "depth": {
      const w = (d.world ?? {}) as Dict;
      out.push(
        kv([
          ["Depth images", m.depth_images],
          ["Fused points", m.points],
          ["TSDF mesh triangles", m.mesh_tris],
          ["Consensus keep (mean)", m.consensus_keep_mean],
          ["Ceiling height", m.ceiling != null ? `${m.ceiling} m` : m.ceiling_source ? "not seen (2.5 m assumed)" : undefined],
          ["Ceiling support (points)", m.ceiling_support_pts],
          ["Manhattan rotation", m.manhattan_deg != null ? `${m.manhattan_deg}° (initial ${m.manhattan_initial_deg}°)` : undefined],
          ["Bounds min", w.bounds ? fmtVal(get(w, "bounds.min")) : undefined],
          ["Bounds max", w.bounds ? fmtVal(get(w, "bounds.max")) : undefined],
        ]),
      );
      break;
    }
    case "splat": {
      out.push(
        kv([
          ["PSNR (held-out)", m.psnr != null ? `${m.psnr} dB` : undefined],
          ["PSNR range", m.psnr_min != null ? `${m.psnr_min} – ${m.psnr_max} dB` : undefined],
          ["PSNR (train sample)", m.train_psnr != null ? `${m.train_psnr} dB` : undefined],
          ["PSNR (colour-corrected)", m.psnr_cc != null ? `${m.psnr_cc} dB` : undefined],
          ["Mean exposure gain", m.app_gain_mean],
          ["SSIM", m.ssim],
          ["Gaussians", m.gaussians],
          ["Steps", m.steps],
          ["Train resolution", Array.isArray(m.train_res) ? (m.train_res as number[]).join("×") : m.train_long_edge],
          ["Train / test views", m.n_train != null ? `${m.n_train} / ${m.n_test}` : undefined],
          ["Views with depth", m.with_depth],
          ["Init points", m.init ? `${get(m, "init.sfm")} SfM + ${get(m, "init.da3") ?? get(m, "init.lidar")} ${get(m, "init.da3") != null ? "DA3 dense" : "LiDAR"}` : undefined],
          ["Training time", fmtDur(num(m.train_s))],
          ["Scene scale", m.scene_scale],
          ["Rollbacks", Array.isArray(m.rollbacks) ? m.rollbacks.length : undefined],
          ["Skipped steps", m.skipped_steps],
          ["Anisotropy reg", m.aniso_reg],
        ]),
      );
      const curve = (d.curve ?? []) as Dict[];
      if (curve.length > 2) {
        out.push(
          h(
            "div",
            { class: "rp-grid2" },
            lineChart(tip, curve.map((c) => ({ x: c.step as number, y: c.l1 as number })), { xLabel: "step", yLabel: "L1 photometric loss", log: true }),
            lineChart(tip, curve.map((c) => ({ x: c.step as number, y: c.depth as number })), { xLabel: "step", yLabel: "LiDAR log-depth loss", log: true }),
          ),
          h(
            "div",
            { class: "rp-grid2" },
            lineChart(tip, curve.map((c) => ({ x: c.step as number, y: c.alive as number })), { xLabel: "step", yLabel: "Visible-opacity Gaussians" }),
            lineChart(tip, curve.map((c) => ({ x: c.step as number, y: c.op_mean as number })), { xLabel: "step", yLabel: "Mean opacity" }),
          ),
        );
      }
      const pv = (m.per_view ?? []) as { view: string; psnr: number; ssim: number }[];
      if (pv.length)
        out.push(
          section(
            "Held-out view PSNR (dB)",
            barChart(
              tip,
              pv.map((p) => ({ label: p.view, value: p.psnr, extra: [["SSIM", fmtVal(p.ssim)]] as [string, string][] })),
              { unit: " dB", ref: num(m.psnr) ? { value: m.psnr as number, label: `mean ${m.psnr}` } : undefined },
            ),
          ),
        );
      if (r.images.length) out.push(section("Held-out views: ground truth (left) | render (right)", images(r.images, "wide")));
      if (Array.isArray(m.rollbacks) && m.rollbacks.length)
        out.push(section("Divergence rollbacks", table(["Step", "EMA", "Best", "LR mult"], (m.rollbacks as Dict[]).map((x) => [x.step, x.ema, x.best, x.lr_mult]))));
      break;
    }
    case "lod": {
      const rm = (m.removed ?? {}) as Dict;
      out.push(
        kv([
          ["Splats in", m.splats_in],
          ["Splats kept", m.splats_kept],
          ["Removed", m.cropped_frac != null ? `${((m.cropped_frac as number) * 100).toFixed(1)} %` : undefined],
          ["…outside room bounds", rm.outside_bounds],
          ["…too large", rm.too_large],
          ["…near-transparent", rm.transparent],
          ["…needle floaters", rm.needles],
          ["LOD levels", m.lod_levels],
          [".rad size", m.rad_mb != null ? `${m.rad_mb} MB` : undefined],
          [".spz size", m.spz_mb != null ? `${m.spz_mb} MB` : undefined],
          ["Kept scale p50 / p99", get(m, "kept_stats.scale_p50_m") != null ? `${get(m, "kept_stats.scale_p50_m")} / ${get(m, "kept_stats.scale_p99_m")} m` : undefined],
          ["Kept opacity p50", get(m, "kept_stats.opacity_p50")],
        ]),
      );
      break;
    }
    case "densify": {
      out.push(
        kv([
          ["Model", m.model],
          ["Mode", m.mode],
          ["Pose-conditioning error", m.pose_fail],
          ["Views / windows", m.views != null ? `${m.views} / ${m.windows}` : undefined],
          ["Depth resolution", Array.isArray(m.depth_res) ? (m.depth_res as number[]).join("×") : undefined],
          ["Dense points", m.points],
          ["Points before fusion", m.points_raw],
          ["Pixels kept", m.kept_frac != null ? `${(Number(m.kept_frac) * 100).toFixed(1)} %` : undefined],
          ["Voxel", m.voxel_m != null ? `${(Number(m.voxel_m) * 1000).toFixed(1)} mm` : undefined],
          ["Observations / point (p50)", m.obs_per_point_p50],
          ["LiDAR scale p10 / p50 / p90", m.lidar_scale_p50 != null ? `${m.lidar_scale_p10} / ${m.lidar_scale_p50} / ${m.lidar_scale_p90}` : undefined],
          ["Views rescaled to LiDAR", m.lidar_aligned_views],
          ["Abs-rel vs LiDAR (before → after)", m.abs_rel_vs_lidar_before != null ? `${(Number(m.abs_rel_vs_lidar_before) * 100).toFixed(1)} % → ${(Number(m.abs_rel_vs_lidar_after) * 100).toFixed(1)} %` : undefined],
          ["Model load / inference", m.infer_s != null ? `${fmtDur(num(m.model_load_s))} / ${fmtDur(num(m.infer_s))}` : undefined],
          ["PLY size", m.ply_mb != null ? `${m.ply_mb} MB` : undefined],
        ]),
      );
      const pv = (d.views ?? []) as { name: string; scale: number | null; kept?: number; abs_rel_before: number | null }[];
      const sc = pv.filter((v) => v.scale != null);
      if (sc.length)
        out.push(
          section(
            "Per-view LiDAR / DA3 depth scale (1.0 = DA3 already metric), in capture order",
            barChart(tip, sc.map((v) => ({ label: v.name, value: v.scale as number })), { ref: { value: 1, label: "1.0" }, yMin: 0.7, yMax: 1.3 }),
          ),
        );
      if (pv.some((v) => v.kept != null))
        out.push(
          section(
            "Per-view pixels kept after confidence + consistency filtering (%)",
            barChart(tip, pv.map((v) => ({ label: v.name, value: (v.kept ?? 0) * 100 })), { unit: " %", yMin: 0, yMax: 100 }),
          ),
        );
      if (r.images.length) out.push(section("RGB | DA3 depth | kept (teal) / dropped (grey)", images(r.images)));
      break;
    }
    case "mesh":
      out.push(
        kv([
          ["Engine", m.engine],
          ["Triangles", m.triangles],
          ["Vertices", m.vertices],
          ["GLB size", m.glb_mb != null ? `${m.glb_mb} MB` : undefined],
          ["Fallback reason", m.fallback_reason],
        ]),
      );
      break;
    case "plan": {
      out.push(
        kv([
          ["Rooms", m.rooms],
          ["Walls", m.walls],
          ["Openings", m.openings],
          ["Furniture", m.furniture],
          ["Total area", m.total_area_m2 != null ? `${m.total_area_m2} m²` : undefined],
          ["Agent turns", m.turns],
          ["Tokens (prompt / output / thinking)", m.tokens ? `${get(m, "tokens.prompt")} / ${get(m, "tokens.output")} / ${get(m, "tokens.thoughts")}` : undefined],
          ["Agent time", fmtDur(num(m.elapsed_s))],
          ["Auto-submitted", m.auto_submitted],
        ]),
      );
      const top = r.images.filter((i) => !i.name.startsWith("agent/"));
      const ag = r.images.filter((i) => i.name.startsWith("agent/"));
      if (top.length) out.push(section("Plan outputs", images(top)));
      if (ag.length) out.push(details(`Agent renders and photos (${ag.length})`, images(ag, "small")));
      const tr = (d.transcript ?? []) as { turn: number; text?: string; calls?: { name: string; args: string }[] }[];
      if (tr.length)
        out.push(
          details(
            `Agent transcript (${tr.length} turns)`,
            h(
              "div",
              { class: "rp-transcript" },
              ...tr.map((t) =>
                h(
                  "div",
                  { class: "rp-turn" },
                  h("span", { class: "n" }, `#${t.turn}`),
                  h(
                    "div",
                    null,
                    t.text ? h("p", null, t.text) : null,
                    ...(t.calls ?? []).map((c) => h("code", null, `${c.name}(${c.args.length > 160 ? `${c.args.slice(0, 160)}…` : c.args})`)),
                  ),
                ),
              ),
            ),
          ),
        );
      break;
    }
    case "publish": {
      out.push(
        kv([
          ["Layers", Array.isArray(m.layers) ? (m.layers as string[]).join(", ") : undefined],
          ["Photos", m.photos],
          ["Partial", m.partial],
        ]),
      );
      const th = (d.photo_thumbs ?? []) as { name: string; url?: string }[];
      if (th.length) out.push(section("Sample keyframes", images(th, "thumbs")));
      break;
    }
  }
  return out;
}

function stageCard(r: StageReport, tip: Tip, maxDur: number): HTMLElement {
  const m = r.metrics ?? {};
  const job = r.job ?? {};
  const all = flatten(m).filter(([k]) => !["per_view", "attempts", "rollbacks"].includes(k.split(".")[0]));
  const head = h(
    "div",
    { class: "rp-card-head" },
    h("div", { class: "rp-card-title" }, h("span", { class: "nm" }, LABEL[r.name] ?? r.name), statusPill(r.status, r.reused, r.version)),
    h(
      "div",
      { class: "rp-card-meta" },
      h("span", null, fmtDur(r.duration_s)),
      h("span", { class: "rp-durbar" }, h("span", { style: { width: `${Math.max(1, ((r.duration_s ?? 0) / (maxDur || 1)) * 100)}%` } })),
    ),
  );
  const body = h(
    "div",
    { class: "rp-card-body" },
    h("p", { class: "rp-blurb" }, BLURB[r.name] ?? ""),
    r.note && r.status !== "done" ? h("p", { class: "rp-note" }, `${r.note}${r.progress != null ? ` · ${Math.round(r.progress * 100)} %` : ""}`) : null,
    r.error ? h("pre", { class: "rp-error" }, r.error) : null,
    ...stageHighlights(r, tip),
    details(
      "Job",
      kv([
        ["Started", fmtTime(r.started_at)],
        ["Ended", fmtTime(r.ended_at)],
        ["Duration", fmtDur(r.duration_s)],
        ["Image", job.image],
        ["Git", job.git_sha],
        ["Config overrides", job.config && Object.keys(job.config).length ? job.config : "none (defaults)"],
        ["Output size", fmtBytes(r.bytes)],
      ]),
    ),
    all.length ? details(`All metrics (${all.length})`, table(["Metric", "Value"], all, "kvt")) : null,
    r.files.length ? details(`Files (${r.files.reduce((a, f) => a + f.files, 0)})`, table(["Path", "Files", "Size"], r.files.map((f) => [f.path, f.files, fmtBytes(f.bytes)]))) : null,
    ...r.logs.map((l) => details(`Log: ${l.name} (${fmtBytes(l.bytes)}, tail)`, h("pre", { class: "rp-log" }, l.tail ?? "(unreadable)"))),
  );
  return h("div", { class: `rp-card ${r.status ?? "pending"}`, "data-stage": r.name }, head, body);
}

function timeline(rep: Report): HTMLElement {
  const st = rep.stages.filter((s_) => s_.started_at && s_.ended_at);
  if (!st.length) return h("div");
  const t0 = Math.min(...st.map((s_) => Date.parse(s_.started_at!)));
  const t1 = Math.max(...st.map((s_) => Date.parse(s_.ended_at!)));
  const span = t1 - t0 || 1;
  return h(
    "div",
    { class: "rp-timeline" },
    ...st.map((s_) => {
      const a = Date.parse(s_.started_at!);
      const b = Date.parse(s_.ended_at!);
      return h(
        "div",
        { class: "rp-tl-row" },
        h("span", { class: "lbl" }, SHORT[s_.name] ?? s_.name),
        h(
          "span",
          { class: "rp-track" },
          h("span", {
            class: `rp-seg ${s_.status ?? ""}`,
            style: { left: `${((a - t0) / span) * 100}%`, width: `${Math.max(0.4, ((b - a) / span) * 100)}%` },
            title: `${s_.name}: ${fmtTime(s_.started_at)} → ${fmtTime(s_.ended_at)} (${fmtDur(s_.duration_s)})`,
          }),
        ),
        h("span", { class: "dur" }, fmtDur(s_.duration_s)),
      );
    }),
  );
}

export function renderReport(rep: Report): HTMLElement {
  const tip = new Tip();
  const maxDur = Math.max(...rep.stages.map((s_) => s_.duration_s ?? 0));
  const total = rep.stages.reduce((a, s_) => a + (s_.reused ? 0 : s_.duration_s ?? 0), 0);
  const up = rep.upload ?? {};
  const root = h(
    "div",
    { class: "report", "data-testid": "report" },
    h(
      "div",
      { class: "rp-summary" },
      kv([
        ["Version", `v${rep.version}${rep.current_version === rep.version ? " (published)" : ""}`],
        ["Status", rep.status],
        ["Compute time (this version)", fmtDur(total)],
        ["Upload", up.bytes != null ? `${fmtBytes(up.bytes as number)} in ${up.files} files` : undefined],
        ["Captured", fmtTime(rep.created_at)],
        ["Report generated", fmtTime(rep.generated_at)],
      ]),
      timeline(rep),
    ),
    ...rep.stages.map((s_) => stageCard(s_, tip, maxDur)),
    tip.el,
  );
  return root;
}

/** Load + render, refreshing while the scan is processing. Returns the container element. */
export function reportPanel(apiBase: string, scanId: string, version?: number): HTMLElement {
  const box = h("div", { class: "rp-panel" }, h("div", { class: "rp-loading" }, "Loading report…"));
  const f = new Fetcher(apiBase);
  let timer = 0;
  const openState = new Map<string, boolean>();
  const load = async () => {
    try {
      const url = `${apiBase}/v1/scans/${encodeURIComponent(scanId)}/report${version ? `?v=${version}` : ""}`;
      const rep = await f.json<Report>(url);
      // keep <details> open/closed state across refreshes
      box.querySelectorAll<HTMLDetailsElement>("details").forEach((d, i) => openState.set(`${i}`, d.open));
      const scrollY = box.closest(".list-page")?.scrollTop;
      box.replaceChildren(renderReport(rep));
      box.querySelectorAll<HTMLDetailsElement>("details").forEach((d, i) => {
        if (openState.has(`${i}`)) d.open = openState.get(`${i}`)!;
      });
      const lp = box.closest(".list-page");
      if (lp && scrollY != null) lp.scrollTop = scrollY;
      const busy = ["queued", "processing", "uploading"].includes(rep.status);
      clearTimeout(timer);
      if (busy) timer = window.setTimeout(() => (box.isConnected ? void load() : undefined), 30000);
    } catch (e) {
      box.replaceChildren(h("div", { class: "rp-loading err" }, `Could not load report: ${e instanceof Error ? e.message : String(e)}`));
    }
  };
  void load();
  return box;
}
