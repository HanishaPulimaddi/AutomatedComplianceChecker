import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/docs": "http://localhost:8000",
      "/envelope": "http://localhost:8000",
      "/site": "http://localhost:8000",
      "/cdc-eligibility": "http://localhost:8000",
      "/address-suggestions": "http://localhost:8000",
      "/grasshopper-plugin": "http://localhost:8000",
    },
  },
});
