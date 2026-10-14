import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` proxies /api to a local VibeVault server (default 127.0.0.1:8787).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": process.env.VIBEVAULT_URL ?? "http://127.0.0.1:8787" } },
  test: { environment: "node" },
});
