import type { Viewer } from "../viewer";
import { LOGO_SVG, fmtDate, h } from "./dom";
import { caret } from "./layersPanel";
import { listScans } from "../data";

export type SidePanel = "layers" | "settings" | "none";

/** Top bar: logo (→ index) · scene switcher · 3D/Plan segmented · layers button · settings gear.
 *  The layers button shows/hides the layers panel; the gear swaps the layers panel for settings (fires "panels"). */
export function header(v: Viewer, opts: { apiBase?: string; onSettings: () => void; onLayers: () => void }): HTMLElement {
  const logo = h("a", { class: "logo", href: "/", title: "All scans" });
  logo.innerHTML = LOGO_SVG;
  const modes = h("span", { class: "seg view-modes", "data-testid": "view-modes" });
  const render = () => {
    modes.replaceChildren(
      h("button", { class: v.mode === "3d" ? "sel" : "", "data-mode": "3d", onclick: () => v.setMode("3d") }, "3D"),
      h("button", { class: v.mode === "plan" ? "sel" : "", "data-mode": "plan", onclick: () => v.setMode("plan") }, "Plan"),
    );
  };
  v.addEventListener("change", render);
  render();

  const menu = h("div", { class: "menu scene-menu" });
  let open = false;
  const switcher = h(
    "button",
    {
      class: "dd scene-switch",
      "data-testid": "scene-switch",
      onclick: async (e: Event) => {
        e.stopPropagation();
        open = !open;
        menu.style.display = open ? "" : "none";
        if (open) await fillMenu(menu, v.scene.scan_id, opts.apiBase);
      },
    },
    h("span", { class: "scan-name", title: `${v.scene.name || v.scene.scan_id} · ${fmtDate(v.scene.captured_at ?? v.scene.created_at)}` }, v.scene.name || v.scene.scan_id),
    h("span", { class: "caret" }, caret()),
  );
  menu.style.display = "none";
  document.addEventListener("click", (e) => {
    if (open && !(e.target as HTMLElement).closest(".scene-menu")) {
      open = false;
      menu.style.display = "none";
    }
  });
  const gear = h("button", { class: "icon-btn gear", "aria-label": "Settings", title: "Settings", "data-testid": "settings", onclick: opts.onSettings });
  gear.innerHTML = GEAR_SVG;
  const layersBtn = h("button", { class: "icon-btn layers-btn sel", "aria-label": "Layers", title: "Layers", "data-testid": "layers-toggle", onclick: opts.onLayers });
  layersBtn.innerHTML = LAYERS_SVG;
  v.addEventListener("panels", (e) => {
    const side = (e as CustomEvent<SidePanel>).detail;
    layersBtn.classList.toggle("sel", side === "layers");
    layersBtn.setAttribute("aria-pressed", String(side === "layers"));
    gear.classList.toggle("sel", side === "settings");
    gear.setAttribute("aria-pressed", String(side === "settings"));
  });
  return h(
    "div",
    { class: "header" },
    logo,
    h("span", { class: "scene-wrap" }, switcher, menu),
    h("span", { class: "spacer" }),
    modes,
    layersBtn,
    gear,
  );
}

async function fillMenu(menu: HTMLElement, current: string, apiBase?: string) {
  const all = h("a", { class: "menu-item", href: "/" }, "All scans");
  menu.replaceChildren(all, h("div", { class: "menu-sep" }), h("div", { class: "menu-note" }, "Loading…"));
  if (!apiBase) {
    menu.replaceChildren(all);
    return;
  }
  try {
    const scans = await listScans(apiBase);
    menu.replaceChildren(
      all,
      h("div", { class: "menu-sep" }),
      ...scans.map((s) =>
        h(
          "a",
          {
            class: `menu-item scene-item ${s.scan_id === current ? "sel" : ""} ${s.status === "ready" ? "" : "disabled"}`,
            href: s.status === "ready" ? s.viewer_url || `/scans/${encodeURIComponent(s.scan_id)}` : "/",
          },
          h("span", { class: `dot ${s.status}` }),
          h("span", { class: "nm" }, s.name || s.scan_id),
          h("span", { class: "meta" }, s.status === "ready" ? fmtDate(s.created_at) : `${s.stage ?? s.status} ${Math.round((s.progress ?? 0) * 100)}%`),
        ),
      ),
    );
  } catch {
    menu.replaceChildren(all, h("div", { class: "menu-sep" }), h("div", { class: "menu-note" }, "Sign in on the index page to switch scans."));
  }
}

const LAYERS_SVG = `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2.5 8 12 13l9.5-5z"/><path d="m2.5 12.5 9.5 5 9.5-5"/><path d="m2.5 17 9.5 5 9.5-5" opacity=".55"/></svg>`;
const GEAR_SVG = `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;

/** Bottom-centre status pill + bottom-right caption + optional stats overlay + walk hint. */
export function statusBar(v: Viewer): HTMLElement[] {
  const status = h("div", { class: "status", "data-testid": "status" });
  const caption = h("div", { class: "caption", "data-testid": "caption" });
  const stats = h("div", { class: "stats-overlay", "data-testid": "stats" });
  const hint = h("div", { class: "walk-hint" }, "walk · WASD move · Q/E down/up · drag to look · shift run · R reset · Esc exits");
  const render = () => {
    const l = v.layers.find((x) => x.id === v.activeLayerId);
    let line = l ? (l.status === "ready" ? l.statusLine() : l.status === "error" ? `${l.def.label} failed: ${l.error}` : `${l.def.label} · loading…`) : "No layers";
    if (v.photo) line = `Photo ${v.photo.id} · ${v.photo.w}×${v.photo.h} · f ${v.photo.fy.toFixed(0)} px · ← → step · Esc exits`;
    status.textContent = line;
    let cap = l?.summary() ?? "";
    if (l?.clipGroup && l.clipped) cap += ` · cropped at ${v.cut.z.toFixed(2)}`;
    caption.textContent = cap;
    caption.style.display = cap ? "" : "none";
    document.body.classList.toggle("walking", v.walking);
  };
  v.addEventListener("change", render);
  v.addEventListener("stats", () => {
    if (!document.body.classList.contains("show-stats")) return;
    const sp = v.splat;
    stats.textContent = `${v.stats.fps.toFixed(0)} fps · ${v.stats.calls} draws · ${(v.stats.tris / 1000).toFixed(0)} k tris${sp ? ` · ${(sp.count / 1000).toFixed(0)} k splats` : ""}`;
  });
  render();
  return [status, caption, stats, hint];
}
