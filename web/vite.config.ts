import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev proxy: /api → selti REST (FastAPI). Port/host overridable via env
// so the frontend never hardcodes a backend address.
const apiTarget = process.env.SELTI_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  base: "/ui/",
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
