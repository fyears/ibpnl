import { defineConfig } from "vite";

// Builds into the backend's static dir so FastAPI serves the production app.
// In dev, proxies API + WebSocket calls to the backend on :8000.
export default defineConfig({
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
