type Child = Node | string | number | null | undefined | false;
type Attrs = Record<string, unknown>;

/** Tiny hyperscript helper: h("div", {class: "x", onclick: fn}, child, …). */
export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Attrs | null = null, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v as EventListener);
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k === "dataset" && typeof v === "object") Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const c of children) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

export const SVG_NS = "http://www.w3.org/2000/svg";

export function s<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Attrs | null = null, ...children: (Node | string | null)[]): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, String(v));
  for (const c of children) if (c != null) el.append(c);
  return el;
}

export function fmtDate(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** Scanner mark: room outline with a doorway, and the camera with its view cone (docs/brand/logo.svg). */
export const LOGO_SVG = `<svg viewBox="0 0 64 64" width="26" height="26" aria-hidden="true">
<rect width="64" height="64" rx="15" fill="#1e1f23"/>
<path d="M27 15 H49 V49 H15 V27" fill="none" stroke="#ECECEE" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M28 36 L43.5 31.8 A16 16 0 0 0 32.1 20.5 Z" fill="#2EC4B0" opacity="0.4"/>
<circle cx="28" cy="36" r="4.6" fill="#2EC4B0"/></svg>`;
