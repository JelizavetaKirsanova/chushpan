import { defineConfig } from "vite";
import {fileURLToPath} from "node:url";

// `npm run dev` serves the HUD with hot reload on :5173 and forwards the events from a
// running `python -m backend voice` (or `hud-demo`) on :8765.
export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    fs: {allow: [fileURLToPath(new URL('.', import.meta.url))]},
    proxy: { "/ws": { target: "ws://127.0.0.1:8765", ws: true } },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
