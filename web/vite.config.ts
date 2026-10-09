import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Bind all interfaces. Vite's default binds localhost, which resolves to ::1 on Windows
    // while curl and container health checks hit 127.0.0.1 -- the same IPv6 trap that kept the
    // ClickHouse container permanently "starting".
    host: true,
    // Proxy keeps the browser same-origin, so no CORS config is needed on the API and the
    // demo works identically whether the API is local or behind an ingress.
    proxy: { "/api": "http://127.0.0.1:8000", "/healthz": "http://127.0.0.1:8000" },
  },
});
