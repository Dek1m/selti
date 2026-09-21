import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev proxy: /api → selti REST (FastAPI). Port/host overridable via env
// so the frontend never hardcodes a backend address.
const apiTarget = process.env.SELTI_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  base: "/ui/",
  // маркер сборки для ?debug=1: мгновенно отличить свежий бандл от старого выката
  define: {
    __BUILD_ID__: JSON.stringify(new Date().toISOString().slice(0, 19).replace("T", " ")),
  },
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
