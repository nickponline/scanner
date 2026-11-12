import { expect, test } from "@playwright/test";

const stage = (name: string, metrics: Record<string, unknown>, data: Record<string, unknown> = {}) => ({
  name, version: 2, reused: false, status: "done", duration_s: 60, started_at: "2026-11-09T08:00:00Z", ended_at: "2026-11-09T08:01:00Z",
  metrics, files: [{ path: "stage.json", files: 1, bytes: 400 }], bytes: 400,
  logs: name === "sfm" ? [{ name: "colmap.log", bytes: 1000, tail: "I2026 done" }] : [], images: [], data,
});

const report = {
  scan_id: "scan_t", name: "Test", version: 2, status: "ready", versions: [1, 2], current_version: 2, created_at: "2026-11-09T07:00:00Z",
  upload: { bytes: 1e8, files: 5 }, generated_at: "2026-11-09T09:00:00Z",
  stages: [
    stage("ingest", { frames: 1800 }),
    stage("frames", { keyframes: 3 }, { frames: [1, 2, 3].map((i) => ({ name: `s00_${i}`, t: i, sharp: 10 * i, depth: i % 2 === 0 })) }),
    stage("sfm", { features: "loma", matcher: "LOMA_G", colmap_registered: 200, images: 240, gate_p95_m: 0.15,
      attempts: { global: { ok: true, registered: 200, p95_m: 0.08, rms_m: 0.04 } } }, { residuals: [["a", 0.02], ["b", 0.05]] }),
    stage("splat", { psnr: 26.1, ssim: 0.9, per_view: [{ view: "a", psnr: 25, ssim: 0.9 }] },
      { curve: [0, 250, 500].map((s) => ({ step: s, l1: 0.1 / (s + 1), depth: 0.05, alive: 1000 + s, op_mean: 0.3 })) }),
    { ...stage("mesh", {}), status: "failed", error: "Traceback: boom" },
  ],
};
const list = [{ scan_id: "scan_t", name: "Test", created_at: report.created_at, status: "ready", stage: "publish", progress: 1, processing_version: 2, current_version: 2 }];

test.beforeEach(async ({ page }) => {
  await page.route((u) => u.pathname === "/v1/scans", (r) => r.fulfill({ json: list }));
  await page.route((u) => u.pathname.startsWith("/v1/") && u.pathname.endsWith("/report"), (r) => r.fulfill({ json: report }));
});

test("index row expands an inline per-stage report", async ({ page }) => {
  await page.goto("/");
  const btn = page.getByTestId("report-toggle");
  await btn.click();
  await expect(btn).toHaveAttribute("aria-expanded", "true");
  const rp = page.getByTestId("report");
  await expect(rp.locator("[data-stage]")).toHaveCount(5);
  await expect(rp.locator("[data-stage=sfm]")).toContainText("200 / 240 (83 %)");
  await expect(rp.locator("[data-stage=sfm]")).toContainText("LOMA_G");
  await expect(rp.locator("[data-stage=splat]")).toContainText("26.1 dB");
  await expect(rp.locator("[data-stage=mesh] .rp-error")).toContainText("boom");
  await expect(rp.locator("[data-stage=splat] svg.rp-chart")).toHaveCount(5); // 4 curves + per-view psnr
  // the row link is not followed when toggling
  await expect(page).toHaveURL(/\/$/);
  await btn.click();
  await expect(page.getByTestId("report")).toHaveCount(0);
});

test("full-page report route", async ({ page }) => {
  await page.goto("/scans/scan_t/report?v=2");
  await expect(page.getByTestId("report").locator("[data-stage=frames]")).toContainText("Keyframe sharpness");
  await page.locator("[data-stage=sfm] summary", { hasText: "Log: colmap.log" }).click();
  await expect(page.locator("[data-stage=sfm] .rp-log")).toContainText("I2026 done");
});
