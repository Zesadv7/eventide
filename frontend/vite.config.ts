import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
const target = process.env.EVENTIDE_API_URL || "http://127.0.0.1:8000";
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/api": {
        target,
        changeOrigin: false,
      },
      "/healthz": { target, changeOrigin: false },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
