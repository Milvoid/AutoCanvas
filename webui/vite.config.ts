import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const proxy = {
    "/_dashboard": {
      target: env.DASHBOARD_API || "http://127.0.0.1:4173",
      changeOrigin: false,
    },
    "/api": {
      target: env.AUTOCANVAS_API || "http://127.0.0.1:8080",
      changeOrigin: false,
    },
    "/health": {
      target: env.AUTOCANVAS_API || "http://127.0.0.1:8080",
      changeOrigin: false,
    },
  };
  return {
    plugins: [vue()],
    server: { proxy },
    preview: {
      host: "127.0.0.1",
      port: 4174,
      strictPort: true,
      allowedHosts: ["localhost"],
      proxy,
    },
  };
});
