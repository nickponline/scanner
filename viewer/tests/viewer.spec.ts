import { expect, test } from "@playwright/test";
import { FIXTURE, layerRow, openViewer, settle, trackErrors } from "./helpers";

test("loads a scene with only a subset of layers", async ({ page }) => {
  const errors = trackErrors(page);
  // Simulate failed stages: drop splat, mesh, furniture, plan and pointcloud from scene.json.
  await page.route((u) => u.pathname === "/fixtures/demo/scene.json", async (route) => {
    const res = await route.fetch();
    const scene = await res.json();
    scene.layers = scene.layers.filter((l: { id: string }) => ["architecture", "photos", "ground", "trajectory"].includes(l.id));
    await route.fulfill({ response: res, json: scene });
  });
  await openViewer(page);
  await expect(page.locator(".layer-row")).toHaveCount(4);
  await expect(page.locator('.layer-row[data-layer="cad"]')).toHaveCount(1);
  await expect(page.locator('.layer-row[data-layer="splat"]')).toHaveCount(0);
  // section slider falls back to the model as the cut target; minimap still renders (no plan)
  await expect(page.getByTestId("minimap")).toHaveClass(/no-plan/);
  const status = await page.evaluate(() => window.viewer!.layers.map((l) => [l.id, l.status]));
  expect(status.filter(([, s]) => s === "error")).toEqual([]);
  expect(errors).toEqual([]);
});

test("layer visibility and mode toggles", async ({ page }) => {
  const errors = trackErrors(page);
  await openViewer(page);
  const arch = await layerRow(page, "cad");
  await expect(arch).toHaveClass(/\bon\b/);
  await arch.locator(".toggle").click();
  await expect(arch).toHaveClass(/\boff\b/);
  expect(await page.evaluate(() => window.viewer!.layer("cad")!.object.visible)).toBe(false);
  await arch.locator(".toggle").click();
  await expect(arch).toHaveClass(/\bon\b/);

  await page.getByTestId("dd-cad").click();
  await page.locator('.menu-item[data-mode="edges"]').click();
  await expect(page.getByTestId("dd-cad")).toContainText("Edges");
  expect(await page.evaluate(() => window.viewer!.layer("cad")!.mode)).toBe("edges");

  // the gear swaps the layers panel for settings (same place); gear again / Esc brings layers back
  await expect(page.getByTestId("settings-panel")).toBeHidden();
  await page.getByTestId("settings").click();
  await expect(page.getByTestId("settings-panel")).toBeVisible();
  await expect(page.getByTestId("layers-panel")).toBeHidden();
  await page.getByTestId("settings").click();
  await expect(page.getByTestId("settings-panel")).toBeHidden();
  await expect(page.getByTestId("layers-panel")).toBeVisible();
  await page.getByTestId("settings").click();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("settings-panel")).toBeHidden();
  await expect(page.getByTestId("layers-panel")).toBeVisible();

  // hidden mesh layer loads lazily when shown
  const mesh = await layerRow(page, "mesh");
  await mesh.locator(".name").click();
  await page.waitForFunction(() => window.viewer!.layer("mesh")!.status === "ready");
  await page.getByTestId("dd-mesh").click();
  await page.locator('.menu-item[data-mode="normals"]').click();
  expect(await page.evaluate(() => window.viewer!.layer("mesh")!.mode)).toBe("normals");

  // plan style picker "1 of 3"
  await expect(page.getByTestId("dd-plan")).toContainText("1 of 3");
  await page.getByTestId("dd-plan").click();
  await page.locator('.menu-item[data-mode="technical"]').click();
  await expect(page.getByTestId("dd-plan")).toContainText("2 of 3");

  // the header layers button hides/shows the panel (no keyboard shortcut any more)
  await page.keyboard.press("l");
  await expect(page.getByTestId("layers-panel")).toBeVisible();
  await page.getByTestId("layers-toggle").click();
  await expect(page.getByTestId("layers-panel")).toBeHidden();
  await page.getByTestId("layers-toggle").click();
  await expect(page.getByTestId("layers-panel")).toBeVisible();
  // from settings, the layers button goes straight back to layers
  await page.getByTestId("settings").click();
  await page.getByTestId("layers-toggle").click();
  await expect(page.getByTestId("settings-panel")).toBeHidden();
  await expect(page.getByTestId("layers-panel")).toBeVisible();

  // deep link reflects state
  await settle(page, 700);
  const url = new URL(page.url());
  expect(url.searchParams.get("layers")).toContain("cad:edges");
  expect(url.searchParams.get("layers")).toContain("plan:technical");
  expect(errors).toEqual([]);
});

test("section slider sets the clip height, snaps and toggles", async ({ page }) => {
  const errors = trackErrors(page);
  await openViewer(page);
  // Gaussian splat is disabled for now: no splat layer is created even though the fixture lists one.
  const d = await page.evaluate(() => ({ on: window.viewer!.cut.on, splat: !!window.viewer!.splat }));
  expect(d.on).toBe(true);
  expect(d.splat).toBe(false);
  const track = page.getByTestId("cut-track");
  const box = (await track.boundingBox())!;
  const [lo, hi] = await page.evaluate(() => window.viewer!.cutRange);
  // click near 1.2 m → snaps exactly to 1.2
  const y = box.y + (1 - (1.22 - lo) / (hi - lo)) * box.height;
  await page.mouse.click(box.x + box.width / 2, y);
  const cut = await page.evaluate(() => ({ ...window.viewer!.cut, clipped: window.viewer!.layer("cad")!.clipped }));
  expect(cut.on).toBe(true);
  expect(cut.z).toBeCloseTo(1.2, 5);
  expect(cut.clipped).toBe(true);
  await expect(page.getByTestId("cut-label")).toHaveText("1.20 · model");
  await expect(page.getByTestId("caption")).toContainText("cropped at 1.20");
  // three.js clip plane keeps y <= 1.2
  const plane = await page.evaluate(() => {
    const p = window.viewer!.clipPlane;
    return [p.normal.y, p.constant];
  });
  expect(plane).toEqual([-1, 1.2]);
  // turn off
  await page.getByTestId("cut-toggle").locator("button", { hasText: "off" }).click();
  expect(await page.evaluate(() => window.viewer!.cut.on)).toBe(false);
  expect(await page.evaluate(() => window.viewer!.layer("cad")!.clipped)).toBe(false);
  // deep link restores the cut
  await openViewer(page, `${FIXTURE}&cut=2.10`);
  expect(await page.evaluate(() => window.viewer!.cut)).toEqual({ on: true, z: 2.1 });
  expect(errors).toEqual([]);
});

test("photo click-through from the minimap, stepping and exit", async ({ page }) => {
  const errors = trackErrors(page);
  await openViewer(page);
  const cams = await page.evaluate(() => window.viewer!.cams!.shown.map((c) => c.id));
  expect(cams.length).toBeGreaterThan(5);
  // click the first camera dot in the minimap
  const dot = page.locator(`.minimap circle.cam-dot[data-cam="${cams[0]}"]`);
  const b = (await dot.boundingBox())!;
  await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
  await expect(page.getByTestId("photo-overlay")).toHaveClass(/open/);
  await expect(page.getByTestId("photo-info")).toContainText(cams[0]);
  await page.waitForFunction(() => document.querySelector<HTMLImageElement>("[data-testid=photo-img]")?.dataset.res === "full");
  // camera pose matches the photo pose (OpenCV c2w → three)
  await settle(page, 1000);
  const err = await page.evaluate(() => {
    const v = window.viewer!;
    const c = v.photo!;
    const p = v.persp.position;
    // World (x,y,z) → three (x, z, −y)
    return Math.hypot(p.x - c.c2w[3], p.y - c.c2w[11], p.z + c.c2w[7]);
  });
  expect(err).toBeLessThan(1e-3);
  await page.keyboard.press("ArrowRight");
  await expect(page.getByTestId("photo-info")).toContainText(cams[1]);
  await page.keyboard.press("ArrowLeft");
  await expect(page.getByTestId("photo-info")).toContainText(cams[0]);
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("photo-overlay")).not.toHaveClass(/open/);
  expect(await page.evaluate(() => window.viewer!.photo)).toBeNull();
  // deep link to a photo
  await openViewer(page, `${FIXTURE}&cam=photo:${cams[2]}`);
  await expect(page.getByTestId("photo-info")).toContainText(cams[2]);
  expect(errors).toEqual([]);
});

test("3D marker click opens a photo; plan mode toggles", async ({ page }) => {
  const errors = trackErrors(page);
  await openViewer(page);
  const cut0 = await page.evaluate(() => window.viewer!.cut.on);
  const pt = await page.evaluate(() => {
    const v = window.viewer!;
    const c = v.cams!.shown[3];
    const THREE_V = v.persp.position.clone().set(c.c2w[3], c.c2w[11], -c.c2w[7]).project(v.persp);
    const r = v.renderer.domElement.getBoundingClientRect();
    return { id: c.id, x: r.left + ((THREE_V.x + 1) / 2) * r.width, y: r.top + ((1 - THREE_V.y) / 2) * r.height };
  });
  await page.mouse.click(pt.x, pt.y);
  await expect(page.getByTestId("photo-info")).toContainText(pt.id);
  await page.keyboard.press("Escape");
  await page.getByTestId("view-modes").locator('button[data-mode="plan"]').click();
  expect(await page.evaluate(() => window.viewer!.mode)).toBe("plan");
  expect(await page.evaluate(() => window.viewer!.cut.on)).toBe(true);
  await page.getByTestId("view-modes").locator('button[data-mode="3d"]').click();
  expect(await page.evaluate(() => window.viewer!.cut.on)).toBe(cut0);
  expect(errors).toEqual([]);
});
