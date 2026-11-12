import type { Viewer } from "../viewer";
import { fitRect } from "../frames";
import type { CameraDef } from "../types";
import { h } from "./dom";

/** Full-frame photo aligned with the 3D view, with render ↔ photo cross-fade and prev/next. */
export class PhotoOverlay {
  readonly el: HTMLElement;
  private img: HTMLImageElement;
  private bars: HTMLElement[];
  private info: HTMLElement;
  private fade: HTMLInputElement;
  private cam: CameraDef | null = null;
  private token = 0;
  private opacity = 1;
  private rooms = new Map<string, string>();

  constructor(private v: Viewer) {
    this.img = h("img", { class: "photo-img", alt: "", "data-testid": "photo-img", draggable: "false" });
    this.bars = [0, 1, 2, 3].map(() => h("div", { class: "lb" }));
    this.info = h("span", { class: "photo-info", "data-testid": "photo-info" });
    this.fade = h("input", { type: "range", min: 0, max: 1, step: 0.01, value: 1, "data-testid": "photo-fade" }) as HTMLInputElement;
    this.fade.addEventListener("input", () => {
      this.opacity = Number(this.fade.value);
      this.img.style.opacity = String(this.opacity);
    });
    const bar = h(
      "div",
      { class: "photo-bar" },
      h("button", { class: "pbtn", title: "previous (←)", onclick: () => v.stepPhoto(-1) }, "‹"),
      this.info,
      h("button", { class: "pbtn", title: "next (→)", onclick: () => v.stepPhoto(1) }, "›"),
      h("span", { class: "sep" }),
      h("span", { class: "fade-label" }, "render"),
      this.fade,
      h("span", { class: "fade-label" }, "photo"),
      h("span", { class: "sep" }),
      h("button", { class: "pbtn close", title: "exit (Esc)", "data-testid": "photo-close", onclick: () => v.exitPhoto() }, "✕"),
    );
    this.el = h("div", { class: "photo-overlay", "data-testid": "photo-overlay" }, ...this.bars, this.img, bar);
    void v.buildingModel.then((bm) => {
      for (const st of bm?.storeys ?? []) for (const r of st.rooms ?? []) this.rooms.set(r.id, r.name);
    });
    v.addEventListener("photo", (e) => this.show((e as CustomEvent<CameraDef | null>).detail));
    window.addEventListener("resize", () => this.layout());
  }

  private show(cam: CameraDef | null) {
    this.cam = cam;
    const tok = ++this.token;
    this.el.classList.toggle("open", !!cam);
    if (!cam) {
      this.img.removeAttribute("src");
      return;
    }
    const cams = this.v.cams!;
    const i = cams.shown.findIndex((c) => c.id === cam.id);
    const room = cam.room_id ? this.roomName(cam.room_id) : "";
    this.info.textContent = `${cam.kind === "hero" ? "hero" : "keyframe"} · ${cam.id}${room ? ` · ${room}` : ""} · ${i + 1} / ${cams.shown.length}`;
    this.layout();
    this.img.classList.remove("ready");
    this.img.style.opacity = "0";
    // progressive: thumbnail first, then full resolution
    const fetcher = this.v.loaded.fetcher;
    void (async () => {
      try {
        const thumb = await fetcher.imageUrl(cams.photoUrl(cam, "thumb"));
        if (tok !== this.token) return;
        this.img.src = thumb;
        this.img.dataset.res = "thumb";
        // wait for the fly-in to finish before showing
        setTimeout(() => {
          if (tok !== this.token) return;
          this.img.classList.add("ready");
          this.img.style.opacity = String(this.opacity);
        }, 820);
        const full = await fetcher.imageUrl(cams.photoUrl(cam, "full"));
        const pre = new Image();
        pre.src = full;
        await pre.decode();
        if (tok !== this.token) return;
        this.img.src = full;
        this.img.dataset.res = "full";
      } catch (e) {
        console.warn("photo load failed", e);
      }
    })();
  }

  private roomName(id: string): string {
    return this.rooms.get(id) ?? "";
  }

  private layout() {
    if (!this.cam) return;
    const W = this.el.clientWidth || window.innerWidth;
    const H = this.el.clientHeight || window.innerHeight;
    const r = fitRect(this.cam.w, this.cam.h, W, H);
    Object.assign(this.img.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
    const [t, b, l, rt] = this.bars;
    Object.assign(t.style, { left: "0", top: "0", width: "100%", height: `${r.y}px` });
    Object.assign(b.style, { left: "0", top: `${r.y + r.h}px`, width: "100%", height: `${H - r.y - r.h}px` });
    Object.assign(l.style, { left: "0", top: `${r.y}px`, width: `${r.x}px`, height: `${r.h}px` });
    Object.assign(rt.style, { left: `${r.x + r.w}px`, top: `${r.y}px`, width: `${W - r.x - r.w}px`, height: `${r.h}px` });
  }
}
