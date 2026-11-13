import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: process.env.REVIEW_PREVIEW_HOST
      ? [process.env.REVIEW_PREVIEW_HOST]
      : [],
    proxy: { "/api": "http://127.0.0.1:8116" },
  },
});
