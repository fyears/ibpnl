import { defineConfig } from "vite";
import { nodePolyfills } from "vite-plugin-node-polyfills";

// Builds into the backend's static dir so FastAPI serves the production app.
// In dev, proxies API + WebSocket calls to the backend on :8000.
export default defineConfig({
  plugins: [
    // plotly.js (source build) imports Node built-ins (buffer/, stream, events)
    // from its image trace. Polyfill them so the browser bundle resolves.
    nodePolyfills({ include: ["buffer", "stream", "events", "util"] }),
  ],
  build: {
    outDir: "../backend/app/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
});
