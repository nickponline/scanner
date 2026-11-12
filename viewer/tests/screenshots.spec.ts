// Visual walkthrough: writes PNGs to viewer/test-screenshots/ for manual review against the reference.
import { test } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { FIXTURE, openViewer, settle, trackErrors } from "./helpers";

// Retina-like captures to compare with the reference screenshot (slow under SwiftShader).
test.use({ deviceScaleFactor: 2 });
test.setTimeout(900_000);

const OUT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../test-screenshots");
const shot = (name: string) => path.join(OUT, `${name}.png`);

test("screenshots", async ({ page }) => {
  const errors = trackErrors(page);
  await openViewer(page);
  await settle(page, 2500);
  await page.screenshot({ path: shot("01-default-3d") });

  await page.getByTestId("view-modes").locator('button[data-mode="plan"]').click();
  await settle(page, 2000);
  await page.screenshot({ path: shot("02-plan-mode") });
  await page.getByTestId("view-modes").locator('button[data-mode="3d"]').click();

  await page.evaluate(() => {
    window.viewer!.setCutZ(1.2);
    window.viewer!.setCutOn(true);
  });
  await settle(page, 2000);
  await page.screenshot({ path: shot("03-section-1.2m") });
  await page.evaluate(() => window.viewer!.setCutOn(false));

  const id = await page.evaluate(() => window.viewer!.cams!.shown.find((c) => c.kind === "hero")!.id);
  await page.evaluate((cid) => window.viewer!.enterPhoto(window.viewer!.cams!.cameras.find((c) => c.id === cid)!), id);
  await page.waitForFunction(() => document.querySelector<HTMLImageElement>("[data-testid=photo-img]")?.dataset.res === "full");
  await settle(page, 1500);
  await page.screenshot({ path: shot("04-photo-full") });
  await page.getByTestId("photo-fade").fill("0.5");
  await settle(page, 600);
  await page.screenshot({ path: shot("05-photo-crossfade-50") });
  await page.getByTestId("photo-fade").fill("0");
  await settle(page, 600);
  await page.screenshot({ path: shot("06-photo-render-only") });
  await page.keyboard.press("Escape");
  await settle(page, 500);

  // layer toggles: solid model + mesh normals, splat off
  const splatRow = page.locator('.layer-row[data-layer="splat"] .toggle'); // splat is disabled for now
  if (await splatRow.count()) await splatRow.click();
  await settle(page, 1500);
  await page.screenshot({ path: shot("07-layers-model-solid") });
  await page.locator('.layer-row[data-layer="cad"] .toggle').click();
  await page.locator('.layer-row[data-layer="mesh"] .name').click();
  await page.waitForFunction(() => window.viewer!.layer("mesh")!.status === "ready");
  await settle(page, 1500);
  await page.screenshot({ path: shot("08-layers-mesh") });
  await page.locator('.layer-row[data-layer="mesh"] .toggle').click();
  await page.locator('.layer-row[data-layer="pointcloud"] .name').click();
  await page.locator('.layer-row[data-layer="trajectory"] .name').click();
  await page.waitForFunction(() => window.viewer!.layer("pointcloud")!.status === "ready");
  await page.getByTestId("settings").click();
  await settle(page, 1500);
  await page.screenshot({ path: shot("09-layers-points-trajectory-settings") });

  // walk mode
  await page.goto("about:blank");
  await openViewer(page);
  await page.keyboard.down("w");
  await page.waitForTimeout(1600);
  await page.keyboard.up("w");
  await settle(page, 800);
  await page.screenshot({ path: shot("10-walk-mode") });

  if (errors.length) throw new Error(`console errors:\n${errors.join("\n")}`);
});

test("reference-style view", async ({ page }) => {
  // cut at 2.47-ish like the reference
  await openViewer(page, `${FIXTURE}&cut=2.40`);
  await settle(page, 2500);
  await page.screenshot({ path: shot("00-reference-style") });
});
