// Docker-only Vite config for the Workspace SPA (frontend/).
// Mounted into the app dir as vite.config.docker.ts — see vite.ucs.config.ts
// for why the targets are service names and why watching polls.
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Plugin } from "vite";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { devGateway } from "./dev-gateway";

const API = process.env.WORKSPACE_API_URL ?? "http://workspace:8080";
const WS = process.env.WORKSPACE_WS_URL ?? "ws://workspace:8080";

// Dev-only: hand the SPA a token so plain http://localhost:5179 works.
//
// Same plugin as the repo's vite.config.ts. There is no control plane locally,
// so the app expects #token=<jwt> on first load; this reads the token the dev
// JWKS stub mints and redirects / -> /?handoff=1#token=<jwt>. The stub's key directory is
// bind-mounted at $HOME/.inaiera-dev-jwks inside this container, so the path the
// plugin already looks in resolves without change.
const devTokenRedirect = (): Plugin => ({
  name: "inaiera-dev-token-redirect",
  apply: "serve",
  configureServer(server) {
    const tokenPath = join(homedir(), ".inaiera-dev-jwks", "token.txt");
    server.middlewares.use((req, res, next) => {
      const url = req.url ?? "/";
      // Only the bare document request — never assets, /api, or /ws.
      const isDocument = url === "/" || url.startsWith("/?");
      // `handoff` marks the redirect below: the token itself rides in the
      // fragment, which the browser never sends back, so without a marker
      // this would redirect forever.
      if (!isDocument || url.includes("token=") || url.includes("handoff=")) return next();
      // Already signed in (the exchange set an HttpOnly workspace_session cookie): don't hand the
      // token over again. Without this, EVERY refresh of "/" redirected through
      // /?handoff=1#token=<jwt>, flashing the token in the address bar each time. A stale cookie
      // shows the "Not signed in" screen, whose "Clear session & reload" drops it and brings this back.
      if (/(?:^|;\s*)workspace_session=/.test(req.headers.cookie ?? "")) return next();
      let token = "";
      try {
        token = readFileSync(tokenPath, "utf8").trim();
      } catch {
        return next(); // stub hasn't minted one yet — show the auth screen
      }
      if (!token) return next();
      res.statusCode = 302;
      // Fragment, not query: never sent to a server, so never logged (WS-AUTH-010).
      res.setHeader("Location", `/?handoff=1#token=${encodeURIComponent(token)}`);
      res.end();
    });
  },
});

// Stands in for Envoy: the backend only trusts gateway-injected identity
// headers + ENVOY_INTERNAL_SECRET. The defaults match the backend's
// docker/docker-compose.yml — change them together. See dev-gateway.ts.
const gateway = devGateway({
  secret: process.env.WORKSPACE_GATEWAY_SECRET ?? "my-demo-secret",
  jwksUrl: process.env.WORKSPACE_JWKS_URL ?? "http://jwks:9999/.well-known/jwks.json",
  issuer: process.env.WORKSPACE_JWT_ISSUER ?? "http://localhost:9999",
  audience: process.env.WORKSPACE_JWT_AUDIENCE ?? "workspace.inaiera",
});

export default defineConfig({
  plugins: [react(), devTokenRedirect(), gateway],
  server: {
    host: true,
    port: 5179,
    watch: { usePolling: true, interval: 300 },
    proxy: {
      "/api": { target: API, changeOrigin: true },
      "/config.json": { target: API, changeOrigin: true },   // runtime config (W-006) from the backend
      "/ws": { target: WS, ws: true, changeOrigin: true },
    },
  },
});
