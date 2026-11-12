import type { Viewer } from "../viewer";
import { h } from "./dom";

/** Right-side vertical section slider: horizontal clip height with floor/ceiling ticks and snapping. */
export class SectionSlider {
  readonly el: HTMLElement;
  private track: HTMLElement;
  private handle: HTMLElement;
  private label: HTMLElement;
  private toggle: HTMLElement;
  private ticks: HTMLElement;
  private dragging = false;

  constructor(private v: Viewer) {
    this.toggle = h("div", { class: "cut-toggle", "data-testid": "cut-toggle" });
    this.ticks = h("div", { class: "ticks" });
    this.label = h("span", { class: "handle-label", "data-testid": "cut-label" });
    this.handle = h("div", { class: "handle", "data-testid": "cut-handle" }, this.label, h("span", { class: "knob" }));
    this.track = h("div", { class: "track", "data-testid": "cut-track" }, h("div", { class: "rail" }), this.ticks, this.handle);
    this.el = h("div", { class: "section-slider", "data-testid": "section" }, this.toggle, this.track);
    this.bind();
    v.addEventListener("change", () => this.render());
    new ResizeObserver(() => this.render()).observe(this.track);
    this.render();
  }

  private get snaps(): { z: number; label?: string }[] {
    const lv = this.v.scene.levels;
    const floor = lv?.floor ?? 0;
    const out: { z: number; label?: string }[] = [{ z: floor, label: `${fmt(floor)} floor` }, { z: floor + 1.2 }];
    if (lv) {
      out.push({ z: lv.ceiling, label: `${fmt(lv.ceiling)} ceiling` });
      if (lv.ceiling_max != null && lv.ceiling_max > lv.ceiling + 0.04) out.push({ z: lv.ceiling_max, label: `${fmt(lv.ceiling_max)} max` });
    }
    return out;
  }

  private zToY(z: number): number {
    const [lo, hi] = this.v.cutRange;
    return (1 - (z - lo) / (hi - lo)) * this.track.clientHeight;
  }

  private yToZ(y: number): number {
    const [lo, hi] = this.v.cutRange;
    return lo + (1 - y / this.track.clientHeight) * (hi - lo);
  }

  private bind() {
    const set = (e: PointerEvent) => {
      const r = this.track.getBoundingClientRect();
      const y = Math.min(Math.max(0, e.clientY - r.top), r.height);
      let z = this.yToZ(y);
      for (const sn of this.snaps) if (Math.abs(this.zToY(sn.z) - y) < 7) z = sn.z;
      this.v.setCutZ(Math.round(z * 100) / 100);
      if (!this.v.cut.on) this.v.setCutOn(true);
    };
    this.track.addEventListener("pointerdown", (e) => {
      this.dragging = true;
      this.track.setPointerCapture(e.pointerId);
      set(e);
    });
    this.track.addEventListener("pointermove", (e) => this.dragging && set(e));
    this.track.addEventListener("pointerup", () => (this.dragging = false));
    this.track.addEventListener("dblclick", () => this.v.setCutZ((this.v.scene.levels?.floor ?? 0) + 1.2));
  }

  private lastSig = "";

  private render() {
    const v = this.v;
    const sig = JSON.stringify([v.cut, v.clipTargets, v.layers.map((l) => l.visible), this.track.clientHeight]);
    if (sig === this.lastSig) return;
    this.lastSig = sig;
    const on = v.cut.on;
    this.toggle.replaceChildren(
      h("span", { class: "pill" }, h("button", { class: on ? "sel" : "", onclick: () => v.setCutOn(true) }, "cut"), " · ", h("button", { class: on ? "" : "sel", onclick: () => v.setCutOn(false) }, "off")),
      h("button", { class: `circle ${on ? "on" : ""}`, "aria-label": "toggle section cut", onclick: () => v.setCutOn(!on) }),
    );
    // labels need ~20 px: skip one that would collide with the handle or an already-placed label (snaps are in
    // priority order: floor, ceiling, then max)
    const placed: number[] = [this.zToY(v.cut.z)];
    this.ticks.replaceChildren(
      ...this.snaps.map((sn) => {
        const y = this.zToY(sn.z);
        const show = !!sn.label && placed.every((p) => Math.abs(p - y) > 20);
        if (show) placed.push(y);
        return h(
          "div",
          { class: `tick ${sn.label ? "" : "minor"}`, style: { top: `${y}px` } },
          show ? h("span", { class: "tick-label" }, sn.label) : null,
          h("span", { class: "tick-line" }),
        );
      }),
    );
    this.handle.style.top = `${this.zToY(v.cut.z)}px`;
    this.handle.classList.toggle("on", on);
    this.label.textContent = `${fmt(v.cut.z)} · ${this.target()}`;
  }

  private target(): string {
    const v = this.v;
    const order: [string, "splat" | "model" | "mesh" | "points"][] = [
      ["splat", "splat"],
      ["model", "model"],
      ["mesh", "mesh"],
      ["points", "points"],
    ];
    for (const [kind, g] of order) {
      const l = v.layers.find((x) => x.clipGroup === g && x.visible && (kind !== "model" || x.def.kind === "model"));
      if (l && v.clipTargets[g]) return kind;
    }
    return v.cut.on ? "cut" : "off";
  }
}

function fmt(z: number): string {
  return Math.abs(z) < 0.005 ? "0" : z.toFixed(2);
}
