import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In production the built files and the API are behind the same nginx, so the
// app can call /api/... directly. This proxy gives `npm run dev` the same
// same-origin behaviour against a locally running API.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
