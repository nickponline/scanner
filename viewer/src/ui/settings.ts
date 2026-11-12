import type { Viewer } from "../viewer";
import type { PointsLayer } from "../layers/points";
import type { CadLayer } from "../layers/models";
import { h } from "./dom";

/** Render settings (formerly "engineering"): opened from the header gear, shown in place of the layers panel. */
export class SettingsPanel {
  readonly el: HTMLElement;
  private body: HTMLElement;

  constructor(private v: Viewer, private onClose: () => void = () => this.toggle(false)) {
    this.body = h("div", { class: "settings-body" });
    this.el = h(
      "div",
      { class: "panel settings hidden", "data-testid": "settings-panel" },
      h(
        "div",
        { class: "panel-head" },
        h("span", { class: "panel-title" }, "Settings"),
        h("button", { class: "icon-btn close", "aria-label": "Close settings", onclick: () => this.onClose() }, "✕"),
      ),
      this.body,
    );
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.open) this.onClose();
    });
  }

  get open() {
    return !this.el.classList.contains("hidden");
  }

  toggle(on = !this.open) {
    this.el.classList.toggle("hidden", !on);
    if (on) this.render();
    this.v.dispatchEvent(new Event("settings"));
  }

  private render() {
    const v = this.v;
    const spark = v.spark as unknown as { lodSplatCount?: number; lodSplatScale: number; falloff?: number };
    const points = v.layerOfKind<PointsLayer>("points");
    const cad = v.layer<CadLayer>("cad");

    const slider = (label: string, min: number, max: number, step: number, value: number, fmt: (x: number) => string, set: (x: number) => void) => {
      const out = h("span", { class: "val" }, fmt(value));
      const input = h("input", { type: "range", min, max, step, value }) as HTMLInputElement;
      input.addEventListener("input", () => {
        const x = Number(input.value);
        out.textContent = fmt(x);
        set(x);
      });
      return h("label", { class: "set-row" }, h("span", { class: "k" }, label), input, out);
    };
    const seg = (label: string, opts: [string, string][], get: () => string, set: (k: string) => void) => {
      const wrap = h("span", { class: "seg" });
      const draw = () =>
        wrap.replaceChildren(
          ...opts.map(([k, l]) =>
            h("button", { class: get() === k ? "sel" : "", onclick: () => (set(k), draw()) }, l),
          ),
        );
      draw();
      return h("div", { class: "set-row" }, h("span", { class: "k" }, label), wrap);
    };
    const check = (label: string, on: boolean, set: (x: boolean) => void, testid?: string) => {
      const cb = h("input", { type: "checkbox", "data-testid": testid }) as HTMLInputElement;
      cb.checked = on;
      cb.addEventListener("change", () => set(cb.checked));
      return h("label", { class: "chk" }, cb, h("span", null, label));
    };

    this.body.replaceChildren(
      ...(v.splat
        ? [
      h("div", { class: "set-group" }, "Splat"),
      slider("Budget", 0.25, 6, 0.25, (spark.lodSplatCount ?? 1.5e6) / 1e6, (x) => `${x.toFixed(2)} M`, (x) => (spark.lodSplatCount = x * 1e6)),
      slider("LOD bias", -2, 2, 0.1, Math.log2(spark.lodSplatScale ?? 1), (x) => `×${Math.pow(2, x).toFixed(2)}`, (x) => (spark.lodSplatScale = Math.pow(2, x))),
      slider("SH degree", 0, 3, 1, 3, (x) => String(x), (x) => v.splat?.setShDegree(x)),
      seg("Draw", [["g", "Gaussian"], ["f", "Flat"]], () => ((spark.falloff ?? 1) < 0.5 ? "f" : "g"), (k) => (spark.falloff = k === "f" ? 0 : 1)),
          ]
        : []),
      ...(cad
        ? [
            h("div", { class: "set-group" }, "CAD"),
            slider("Fill opacity", 0, 1, 0.05, cad.fillOpacity, (x) => `${Math.round(x * 100)}%`, (x) => cad.setFillOpacity(x)),
          ]
        : []),
      h("div", { class: "set-group" }, "Points"),
      slider("Point size", 1, 8, 0.5, points?.pointSize ?? 2, (x) => `${x} px`, (x) => v.layers.filter((l) => l.def.kind === "points").forEach((l) => (l as PointsLayer).setPointSize(x))),
      h("div", { class: "set-group" }, "Section cut applies to"),
      h(
        "div",
        { class: "chks" },
        ...((v.splat ? ["splat", "model", "mesh", "points"] : ["model", "mesh", "points"]) as ("splat" | "model" | "mesh" | "points")[]).map((g) =>
          check(g === "model" ? "CAD" : g[0].toUpperCase() + g.slice(1), v.clipTargets[g], (on) => v.setClipTarget(g, on)),
        ),
      ),
      h("div", { class: "set-group" }, "Diagnostics"),
      h(
        "div",
        { class: "chks" },
        check("Stats overlay", document.body.classList.contains("show-stats"), (on) => document.body.classList.toggle("show-stats", on), "stats-toggle"),
      ),
    );
  }
}
