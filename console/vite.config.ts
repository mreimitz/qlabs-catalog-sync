import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Tailwind v4 runs as a Vite plugin. Remove it and src/styles.css is never
// processed — the app builds, and renders completely UNSTYLED. See
// docs/CONSUMING.md §4 in the brand-ui repo.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173 },
});
