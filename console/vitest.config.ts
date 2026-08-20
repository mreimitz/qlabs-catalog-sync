import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Minimal, standalone from vite.config.ts on purpose: the app's own Vite config runs
// the Tailwind v4 plugin to process src/styles.css for the built bundle, which no test
// here needs (component tests render markup, not computed styles — visual/contrast
// verification is the brand-ui-audit skill's job, not this suite's). Keeping `css:
// false` means no test pays that build cost. T13.8 owns refining this file further;
// this is the minimum that makes `pnpm test` / `pnpm a11y` real rather than absent.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
