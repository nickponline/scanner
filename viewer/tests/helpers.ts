import { expect, type Page } from "@playwright/test";

export const FIXTURE = "/?scene=/fixtures/demo/scene.json";

/** Collect console errors / page errors for the test. */
export function trackErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

/** Load the viewer and wait until every visible layer has finished loading, plus a few frames. */
export async function openViewer(page: Page, url = FIXTURE) {
  await page.goto(url);
  await page.waitForSelector("body[data-ready='1']", { timeout: 60_000 });
  await page.waitForFunction(
    () => {
      const v = window.viewer;
      return !!v && v.layers.every((l) => !l.visible || l.status !== "loading");
    },
    null,
    { timeout: 60_000 },
  );
  await settle(page);
}

export async function settle(page: Page, ms = 1500) {
  // allow tweens, splat sort and texture uploads to finish
  await page.waitForTimeout(ms);
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
}

export async function layerRow(page: Page, id: string) {
  const row = page.locator(`.layer-row[data-layer="${id}"]`);
  await expect(row).toBeVisible();
  return row;
}
