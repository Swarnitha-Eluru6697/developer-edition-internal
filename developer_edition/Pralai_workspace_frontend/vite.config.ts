import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Plugin } from "vite";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import federation from "@originjs/vite-plugin-federation";
import { devGateway } from "./dev-gateway";

// Dev-only: hand the SPA a token so plain http://localhost:5179 works.
//
// There is no control plane locally, so the app expects #token=<jwt> on first
// load (fragment only: a ?token= query string is never accepted, W-005). Pasting that URL by hand is easy to forget — and once the stored token
// is cleared, a bare reload lands on "No JWT provided". This reads the token
// the dev JWKS stub writes at startup and redirects / → /#token=<jwt>.
//
// Dev server only: it never runs in `vite build`, so nothing reaches prod,
// where the real control plane issues the token.
const devTokenRedirect = (): Plugin => ({
  name: "inaiera-dev-token-redirect",
  apply: "serve",
  configureServer(server) {
    const tokenPath = join(homedir(), ".inaiera-dev-jwks", "token.txt");
    server.middlewares.use((req, res, next) => {
      const url = req.url ?? "/";
      // Only the bare document request — never assets, /api, or /ws.
      const isDocument = url === "/" || url.startsWith("/?");
      if (!isDocument || url.includes("token=") || url.includes("handoff=")) return next();
      let token = "";
      try {
        token = readFileSync(tokenPath, "utf8").trim();
      } catch {
        return next(); // no stub token on this machine — show the auth screen
      }
      if (!token) return next();
      res.statusCode = 302;
      res.setHeader("Location", `/?handoff=1#token=${encodeURIComponent(token)}`);
      res.end();
    });
  },
});

// Dev server for the Workspace SPA.
//
// In production the built `dist/` is baked into the backend image and served
// from `/`, so API calls are same-origin. Locally we run Vite on :5179 and
// proxy the same relative paths to the backend on :8080 so `/api/v1/*` and the
// `/ws/*` WebSocket resolve exactly as they would in prod.
// Stands in for Envoy: the backend only trusts gateway-injected identity
// headers + ENVOY_INTERNAL_SECRET. The defaults match the backend's
// docker/docker-compose.yml — change them together. See dev-gateway.ts.
const gateway = devGateway({
  secret: process.env.WORKSPACE_GATEWAY_SECRET ?? "my-demo-secret",
  jwksUrl: process.env.WORKSPACE_JWKS_URL ?? "http://localhost:9999/.well-known/jwks.json",
  issuer: process.env.WORKSPACE_JWT_ISSUER ?? "http://localhost:9999",
  audience: process.env.WORKSPACE_JWT_AUDIENCE ?? "workspace.inaiera",
});

export default defineConfig({
  // react-redux uses use-sync-external-store (CommonJS), which would bundle a private React copy
  // into the remote and crash hooks inside the portal. Route it to ESM files using the shared React.
  resolve: {
    alias: [
      { find: /^use-sync-external-store\/(shim\/)?with-selector(\.js)?$/, replacement: fileURLToPath(new URL("./src/federation/useSyncExternalStoreWithSelector.ts", import.meta.url)) },
      { find: /^use-sync-external-store\/shim(\/index)?(\.js)?$/, replacement: fileURLToPath(new URL("./src/federation/useSyncExternalStoreShim.ts", import.meta.url)) },
    ],
  },
  plugins: [
    react(),
    devTokenRedirect(),
    gateway,
    // Workspace ships as a Module-Federation REMOTE: the Control Portal loads
    // remoteEntry.js through the tunnel and mounts `WorkspaceApp`. The
    // standalone index.html build still works alongside it.
    federation({
      name: "workspace_mfe",
      filename: "remoteEntry.js",
      exposes: {
        "./WorkspaceApp": "./src/WorkspaceApp.tsx",
      },
      // Must match the host's shared list and React major (18).
      shared: ["react", "react-dom"],
    }),
  ],
  // Federation output uses top-level await, so esnext is required. One CSS file
  // so the remote's styles load with it.
  build: { target: "esnext", cssCodeSplit: false },
  // The icon font is referenced from the bundled CSS. Left alone Vite writes root-absolute urls (`/assets/…woff2`), which inside the
  // portal resolve against the PORTAL's origin and 404. Relative to the CSS file they follow wherever the remote is served from.
  // CSS only: the JS chunks are untouched (federation resolves those from remoteEntry.js's own address).
  experimental: {
    renderBuiltUrl(_filename, { hostType }) {
      return hostType === "css" ? { relative: true } : undefined;
    },
  },
  server: {
    host: true,
    port: 5179,
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      "/config.json": { target: "http://localhost:8080", changeOrigin: true },   // runtime config (W-006) from the backend
      "/ws": {
        target: "ws://localhost:8080",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
