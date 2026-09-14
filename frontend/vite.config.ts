import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite serves the React app on :5173 and proxies /api + /api/runs SSE
// to the FastAPI backend on :18765. Prod: `npm run build` emits dist/, which
// FastAPI serves at /ui (see backend api/app.py). `base: "/ui/"` makes the
// built index.html reference assets at /ui/assets/* so they resolve under the
// /ui mount point.
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:18765",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
