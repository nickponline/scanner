import "./styles.css";
import { AuthError, listScans, loadScene, parseSource, setToken, type ScanSummary, type Source } from "./data";
import { applyDeepLink, readDeepLink, syncDeepLink } from "./deeplink";
import { Viewer } from "./viewer";
import { LayersPanel } from "./ui/layersPanel";
import { Minimap } from "./ui/minimap";
import { SectionSlider } from "./ui/section";
import { PhotoOverlay } from "./ui/photo";
import { header, statusBar, type SidePanel } from "./ui/chrome";
import { SettingsPanel } from "./ui/settings";
import { LOGO_SVG, fmtDate, h } from "./ui/dom";
import { reportPanel } from "./ui/report";

const app = document.getElementById("app")!;

declare global {
  interface Window {
    viewer?: Viewer;
  }
}

async function start() {
  const src = parseSource();
  try {
    if (src.mode === "list") await showList(src);
    else if (src.mode === "report") showReportPage(src);
    else await showViewer(src);
  } catch (e) {
    if (e instanceof AuthError) tokenPrompt();
    else showError(e);
  }
}

async function showViewer(src: Source) {
  const loaded = await loadScene(src);
  document.title = `${loaded.scene.name || "Scan"} · Scanner`;
  const stage = h("div", { class: "stage" });
  app.replaceChildren(stage);
  const v = new Viewer(stage, loaded);
  window.viewer = v;
  const panel = new LayersPanel(v);
  // one side panel at a time under the header: layers, settings (in place of layers) or none
  let side: SidePanel = "layers";
  const setSide = (s: SidePanel) => {
    side = s;
    panel.setHidden(s !== "layers");
    if (settings.open !== (s === "settings")) settings.toggle(s === "settings");
    v.dispatchEvent(new CustomEvent<SidePanel>("panels", { detail: s }));
  };
  const settings = new SettingsPanel(v, () => setSide("layers"));
  const apiBase = src.mode === "api" ? src.apiBase : undefined;
  const ui = h(
    "div",
    { class: "ui" },
    h("div", { class: "top-left" }, header(v, {
        apiBase,
        onSettings: () => setSide(side === "settings" ? "layers" : "settings"),
        onLayers: () => setSide(side === "layers" ? "none" : "layers"),
      }), panel.el, settings.el),
    new Minimap(v).el,
    new SectionSlider(v).el,
    ...statusBar(v),
  );
  stage.append(new PhotoOverlay(v).el, ui);
  await applyDeepLink(v, readDeepLink());
  syncDeepLink(v);
  document.body.dataset.ready = "1";
}

const STAGE_LABEL: Record<string, string> = {
  ingest: "Ingest", frames: "Frames", sfm: "SfM", depth: "Depth", splat: "Splat",
  densify: "Densify", lod: "LOD", mesh: "Mesh", plan: "CAD", publish: "Publish",
};

function showReportPage(src: Extract<Source, { mode: "report" }>) {
  document.title = "Processing report · Scanner";
  const logo = h("a", { class: "logo", href: "/" });
  logo.innerHTML = LOGO_SVG;
  const ver = src.version ? parseInt(src.version, 10) : undefined;
  app.replaceChildren(
    h(
      "div",
      { class: "list-page report-page" },
      h(
        "div",
        { class: "list-head" },
        logo,
        h("a", { class: "list-title crumb", href: "/" }, "Scans"),
        h("span", { class: "list-title sep" }, "/"),
        h("span", { class: "list-title" }, `Report · ${src.scanId}${ver ? ` · v${ver}` : ""}`),
        h("a", { class: "rp-open", href: `/scans/${encodeURIComponent(src.scanId)}${ver ? `?v=${ver}` : ""}` }, "Open viewer"),
      ),
      reportPanel(src.apiBase, src.scanId, ver),
    ),
  );
  document.body.dataset.ready = "1";
}

// scan ids whose inline report is expanded on the index page (kept across list refreshes)
const openReports = new Map<string, HTMLElement>();

async function showList(src: Extract<Source, { mode: "list" }>) {
  document.title = "Scans · Scanner";
  const logo = h("a", { class: "logo", href: "/" });
  logo.innerHTML = LOGO_SVG;
  const count = h("span", { class: "count" });
  const body = h("div", { class: "scan-list", "data-testid": "scan-list" });
  app.replaceChildren(
    h(
      "div",
      { class: "list-page" },
      h("div", { class: "list-head" }, logo, h("span", { class: "list-title" }, "Scans"), count),
      h(
        "div",
        { class: "scan-table" },
        h(
          "div",
          { class: "scan-row head" },
          h("span", null, ""),
          h("span", null, "Scene"),
          h("span", null, "Pipeline"),
          h("span", { class: "r" }, "Progress"),
          h("span", null, ""),
        ),
        body,
      ),
    ),
  );
  let timer = 0;
  const refresh = async () => {
    const scans = await listScans(src.apiBase);
    count.textContent = `${scans.length}`;
    body.replaceChildren(
      ...scans.flatMap((s) => {
        const row = scanRow(s, src.apiBase, body);
        const rp = openReports.get(s.scan_id);
        return rp ? [row, rp] : [row];
      }),
    );
    if (!scans.length) body.append(h("div", { class: "empty" }, "No scans yet. Capture one with the Scanner app."));
    document.body.dataset.ready = "1";
    clearTimeout(timer);
    const busy = scans.some((s) => !["ready", "failed"].includes(s.status));
    timer = window.setTimeout(() => void refresh().catch(console.warn), busy ? 8000 : 30000);
  };
  await refresh();
}

function toggleReport(s: ScanSummary, apiBase: string, row: HTMLElement, btn: HTMLElement) {
  const cur = openReports.get(s.scan_id);
  if (cur) {
    cur.remove();
    openReports.delete(s.scan_id);
    btn.classList.remove("sel");
    btn.setAttribute("aria-expanded", "false");
    return;
  }
  const ver = s.processing_version ?? s.current_version ?? undefined;
  const wrap = h(
    "div",
    { class: "scan-report", "data-report": s.scan_id },
    h(
      "div",
      { class: "scan-report-head" },
      h("span", null, `Processing report · v${ver ?? "?"}`),
      h("a", { href: `/scans/${encodeURIComponent(s.scan_id)}/report${ver ? `?v=${ver}` : ""}`, target: "_blank", rel: "noopener" }, "Open full page ↗"),
    ),
    reportPanel(apiBase, s.scan_id, ver),
  );
  openReports.set(s.scan_id, wrap);
  row.after(wrap);
  btn.classList.add("sel");
  btn.setAttribute("aria-expanded", "true");
}

function scanRow(s: ScanSummary, apiBase: string, _body: HTMLElement): HTMLElement {
  const ready = s.status === "ready";
  const pct = Math.round((s.progress ?? 0) * 100);
  const states = s.stage_states ?? {};
  // mesh is retired: only shown for older scans that ran it
  const stages = Object.keys(STAGE_LABEL).filter((k) => k !== "mesh" || states[k] === "done").map((k) =>
    h(
      "span",
      { class: `stg ${states[k] ?? "pending"} ${s.stage === k && !ready ? "cur" : ""}`, title: `${STAGE_LABEL[k]} · ${states[k] ?? "pending"}` },
      h("span", { class: "bar" }),
      h("span", { class: "lbl" }, STAGE_LABEL[k]),
    ),
  );
  const statusText =
    s.status === "processing" || s.status === "queued"
      ? `${STAGE_LABEL[s.stage ?? ""] ?? s.status}${s.progress_note ? ` · ${s.progress_note}` : ""}`
      : s.status === "failed"
        ? `Failed${s.failed_stage ? ` at ${STAGE_LABEL[s.failed_stage] ?? s.failed_stage}` : ""}`
        : s.status === "ready"
          ? `Ready${s.partial ? " · partial" : ""}`
          : s.status;
  const open = openReports.has(s.scan_id);
  const reportBtn = h(
    "button",
    { class: `rp-btn ${open ? "sel" : ""}`, type: "button", "aria-expanded": open ? "true" : "false", "data-testid": "report-toggle", title: "Processing report" },
    "Report",
  );
  const row = h(
    ready ? "a" : "div",
    {
      class: `scan-row ${s.status}`,
      href: ready ? s.viewer_url || `/scans/${encodeURIComponent(s.scan_id)}` : null,
      "data-scan": s.scan_id,
    },
    s.thumbnail_url ? h("img", { class: "thumb", src: s.thumbnail_url, alt: "" }) : h("span", { class: "thumb ph" }),
    h(
      "span",
      { class: "scene" },
      h("span", { class: "nm" }, s.name || s.scan_id),
      h("span", { class: "meta" }, `${fmtDate(s.created_at)} · v${s.processing_version ?? s.current_version ?? 1}`),
    ),
    h("span", { class: "stages" }, ...stages),
    h(
      "span",
      { class: "prog" },
      h("span", { class: `badge ${s.status}` }, statusText),
      h("span", { class: "pbar" }, h("span", { class: "fill", style: { width: `${pct}%` } })),
      h("span", { class: "pct" }, `${pct}%`),
    ),
    reportBtn,
  );
  reportBtn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    toggleReport(s, apiBase, row, reportBtn);
  });
  return row;
}

function tokenPrompt() {
  const input = h("input", { type: "password", placeholder: "API token", autocomplete: "off" }) as HTMLInputElement;
  const form = h(
    "form",
    {
      class: "card token",
      onsubmit: (e: Event) => {
        e.preventDefault();
        setToken(input.value.trim());
        void start();
      },
    },
    h("div", { class: "card-title" }, "Scanner"),
    h("p", null, "Enter an access token to view scans. It is stored in this browser only."),
    input,
    h("button", { type: "submit" }, "Continue"),
  );
  app.replaceChildren(h("div", { class: "center" }, form));
  input.focus();
}

function showError(e: unknown) {
  console.error(e);
  const msg = e instanceof Error ? e.message : String(e);
  app.replaceChildren(
    h(
      "div",
      { class: "center" },
      h("div", { class: "card" }, h("div", { class: "card-title" }, "Could not load scan"), h("p", { class: "err" }, msg)),
    ),
  );
}

void start();
