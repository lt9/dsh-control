import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 后端单容器同端口 serve API 与静态页（契约 §7）；dev 用 proxy 对齐同一路径。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 13301,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:13300",
        changeOrigin: false,
      },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
