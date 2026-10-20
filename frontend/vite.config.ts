import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 后端单容器同端口 serve API 与静态页（契约 §7）；dev 用 proxy 对齐同一路径。
// VITE_PORT / VITE_API_TARGET 可临时改指向（如假接口演练），默认按契约 13301 → 13300。
export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.VITE_PORT ?? 13301),
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://127.0.0.1:13300",
        changeOrigin: false,
      },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
