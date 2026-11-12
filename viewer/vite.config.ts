import { defineConfig, type Plugin } from "vite";
import { rmSync } from "node:fs";
import { resolve } from "node:path";

// public/fixtures is for dev/tests only; don't ship it in dist unless KEEP_FIXTURES=1.
function dropFixtures(): Plugin {
  let outDir = "dist";
  return {
    name: "drop-fixtures",
    apply: "build",
    configResolved(c) {
      outDir = c.build.outDir;
    },
    closeBundle() {
      if (!process.env.KEEP_FIXTURES) rmSync(resolve(outDir, "fixtures"), { recursive: true, force: true });
    },
  };
}

// Served by the API at "/" (scan list) and "/scans/{id}" (SPA fallback), so assets use base "/".
export default defineConfig({
  base: "/",
  plugins: [dropFixtures()],
  server: { port: 5173, strictPort: true },
  preview: { port: 4173, strictPort: true },
  build: { target: "es2022", chunkSizeWarningLimit: 4000 },
  optimizeDeps: { exclude: ["@sparkjsdev/spark"] },
});
