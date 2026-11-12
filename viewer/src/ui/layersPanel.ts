import type { Viewer } from "../viewer";
import type { Layer } from "../layers/base";
import { PlanLayer } from "../layers/plan";
import { PointsLayer } from "../layers/points";
import { h } from "./dom";

/** Colour key per layer (matches what the layer draws). */
const SWATCH: Record<string, string> = {
  splat: "#d9dce1",
  cad: "#9aa0a8",
  mesh: "#9aa0a8",
  pointcloud: "#7d828b",
  dense: "#b4b8bf",
  photos: "#2ec4b0",
  trajectory: "#b9bec8",
  plan: "#e6e8eb",
  ground: "#5b6068",
};
const KIND_SWATCH: Record<string, string> = {
  model: "#9aa0a8",
  splat: "#d9dce1",
  mesh: "#9aa0a8",
  points: "#7d828b",
  cameras: "#2ec4b0",
  trajectory: "#b9bec8",
  plan: "#e6e8eb",
  ground: "#5b6068",
};

const MODE_LABEL: Record<string, string> = {
  keyframes: "Keyframes",
  hero: "Hero",
  all: "All",
  culled: "Hide back",
  color: "Colour",
  normals: "Normals",
  wire: "Wire",
  solid: "Solid",
  edges: "Edges",
  ceilings: "+ Ceilings",
};

function modeLabel(l: Layer, m: string) {
  if (l instanceof PlanLayer) return `${m[0].toUpperCase()}${m.slice(1)}`;
  if (l instanceof PointsLayer && m === "all") return "All";
  return MODE_LABEL[m] ?? `${m[0]?.toUpperCase() ?? ""}${m.slice(1)}`;
}

/** Top-left layers panel: one aligned row per layer — toggle · swatch · name/summary · mode. */
export class LayersPanel {
  readonly el: HTMLElement;
  private rows: HTMLElement;
  private openMenu: string | null = null;
  private lastSig = "";

  constructor(private v: Viewer) {
    this.rows = h("div", { class: "layer-rows" });
    this.el = h(
      "div",
      { class: "panel layers", "data-testid": "layers-panel" },
      h("div", { class: "panel-head" }, h("span", { class: "panel-title" }, "Layers")),
      this.rows,
    );
    v.addEventListener("change", () => this.render());
    document.addEventListener("click", (e) => {
      if (this.openMenu && !(e.target as HTMLElement).closest(".menu, .dd")) {
        this.openMenu = null;
        this.render(true);
      }
    });
    this.render();
  }

  setHidden(hidden: boolean) {
    this.el.classList.toggle("hidden", hidden);
  }

  private render(force = false) {
    const v = this.v;
    const sig = JSON.stringify([
      v.layers.map((l) => [l.id, l.visible, l.mode, l.status, l.summary()]),
      v.activeLayerId,
      v.cut,
      v.clipTargets,
      this.openMenu,
    ]);
    if (sig === this.lastSig && !force) return;
    this.lastSig = sig;
    this.rows.replaceChildren(...v.layers.map((l) => this.row(l)));
  }

  private row(l: Layer): HTMLElement {
    const sw = SWATCH[l.id] ?? KIND_SWATCH[l.def.kind] ?? "#aaa";
    const toggle = (e: Event) => {
      e.stopPropagation();
      l.setVisible(!l.visible);
      if (l.visible) this.v.setActiveLayer(l.id);
    };
    const cls = ["layer-row", l.visible ? "on" : "off", this.v.activeLayerId === l.id ? "active" : "", l.status].join(" ");
    let summary = l.summary();
    if (l.status === "loading" && l.visible) summary = summary ? `Loading · ${summary}` : "Loading…";
    if (l.clipGroup && l.clipped && l.visible) summary += ` · cut ${this.v.cut.z.toFixed(2)} m`;
    return h(
      "div",
      { class: cls, "data-layer": l.id, title: l.error ?? "", onclick: () => this.v.setActiveLayer(l.id) },
      h(
        "button",
        { class: "toggle", role: "switch", "aria-checked": String(l.visible), "aria-label": `Toggle ${l.def.label}`, onclick: toggle },
        h("span", { class: "knob" }),
      ),
      h("span", { class: "swatch", style: { background: sw } }),
      h(
        "span",
        { class: "text", onclick: toggle },
        h("span", { class: "name" }, l.def.label),
        h("span", { class: "summary" }, summary),
      ),
      this.modes(l),
    );
  }

  private modes(l: Layer): HTMLElement {
    const modes = l.modes;
    if (modes.length < 2) return h("span", { class: "modes" });
    const label = l instanceof PlanLayer ? l.styleLabel() : modeLabel(l, l.mode ?? modes[0]);
    const open = this.openMenu === l.id;
    return h(
      "span",
      { class: "modes" },
      h(
        "button",
        {
          class: `dd ${open ? "open" : ""}`,
          "data-testid": `dd-${l.id}`,
          onclick: (e: Event) => {
            e.stopPropagation();
            this.openMenu = open ? null : l.id;
            this.render(true);
          },
        },
        h("span", { class: "dd-label" }, label),
        h("span", { class: "caret" }, caret()),
      ),
      open
        ? h(
            "div",
            { class: "menu" },
            ...modes.map((m) =>
              h(
                "button",
                {
                  class: `menu-item ${l.mode === m ? "sel" : ""}`,
                  "data-mode": m,
                  onclick: (e: Event) => {
                    e.stopPropagation();
                    this.openMenu = null;
                    l.setMode(m);
                    if (!l.visible) l.setVisible(true);
                    this.render(true);
                  },
                },
                modeLabel(l, m),
              ),
            ),
          )
        : null,
    );
  }
}

export function caret(): Node {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 10 10");
  s.setAttribute("width", "9");
  s.setAttribute("height", "9");
  s.innerHTML = '<path d="M2 3.5 L5 6.5 L8 3.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>';
  return s;
}
