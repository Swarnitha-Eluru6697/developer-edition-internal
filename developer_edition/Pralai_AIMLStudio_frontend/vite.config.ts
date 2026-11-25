import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import federation from "@originjs/vite-plugin-federation";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const BACKEND = "http://localhost:4001";

// Every mlstudio:* permission (backend permissions.json) — the dev default, so
// a developer sees every page. Narrow it with DEV_GATEWAY_PERMISSIONS.
const ALL_MLSTUDIO_PERMISSIONS = [
  "mlstudio:access:view", "mlstudio:dashboard:view", "mlstudio:model-overview:view",
  "mlstudio:model-catalog:view", "mlstudio:datasets:view", "mlstudio:datasets:upload",
  "mlstudio:experiments:view", "mlstudio:train:run", "mlstudio:gpu:train-large",
  "mlstudio:pipelines:view", "mlstudio:clusters:view", "mlstudio:compute:approve",
  "mlstudio:deploy:promote", "mlstudio:endpoints:view", "mlstudio:models:delete",
  "mlstudio:models:share", "mlstudio:monitoring:edit", "mlstudio:alerts:view",
  "mlstudio:alerts:edit", "mlstudio:base-models:sync",
].join(",");

// Stand in for Envoy in standalone dev: the backend answers 401 to anything
// that didn't come through the gateway (TRUST_ENVOY_HEADERS=true + matching
// x-inaiera-gateway-secret). The browser sends no identity; the dev PROXY adds
// it here, the way Envoy would from a verified ticket. Dev server only — no
// effect on a build. Secret: DEV_GATEWAY_SECRET, else ENVOY_INTERNAL_SECRET
// from ../Pralai_AIMLStudio_backend/.env; with neither, nothing is injected.
// Override who you arrive as with DEV_GATEWAY_USER / _USER_ID / _TENANT /
// _GROUPS / _PERMISSIONS (comma-separated).
function devGatewaySecret(): string {
  if (process.env.DEV_GATEWAY_SECRET) return process.env.DEV_GATEWAY_SECRET;
  try {
    const env = readFileSync(resolve(__dirname, "../Pralai_AIMLStudio_backend/.env"), "utf8");
    return env.match(/^ENVOY_INTERNAL_SECRET=(.*)$/m)?.[1].trim() ?? "";
  } catch {
    return "";
  }
}

function withGatewayHeaders(target: string) {
  const secret = devGatewaySecret();
  if (!secret) return target;
  const user = process.env.DEV_GATEWAY_USER || "dev-user";
  return {
    target,
    headers: {
      "x-inaiera-gateway-secret": secret,
      "x-user-name": user,
      "x-user-id": process.env.DEV_GATEWAY_USER_ID || user,
      "x-user-permissions": process.env.DEV_GATEWAY_PERMISSIONS || ALL_MLSTUDIO_PERMISSIONS,
      // ml-studio-engineers holds UCS write on ml_models.default.* (promote,
      // rollback, register versions); data-engineers reads the shared datasets.
      "x-user-groups": process.env.DEV_GATEWAY_GROUPS || "ml-studio-engineers,data-engineers",
      "x-tenant-id": process.env.DEV_GATEWAY_TENANT || "prod_metalake",
    },
  };
}

export default defineConfig({
  plugins: [
    react(),
    // ML Studio ships as a Module-Federation REMOTE, the same way Catalog and
    // DataConnect do: the Control Portal loads remoteEntry.js through the
    // tunnel and mounts `MLStudioApp` in a panel. Dropping this turns the build
    // back into a plain SPA and the host's import 404s — the standalone
    // index.html build still works alongside it.
    federation({
      name: "mlstudio_mfe",
      filename: "remoteEntry.js",
      exposes: {
        "./MLStudioApp": "./src/MLStudioApp.tsx",
      },
      // Must match the host's shared list and React major (18), so one runtime
      // is shared rather than two copies fighting over hooks and context.
      shared: ["react", "react-dom"],
    }),
  ],
  // Federation output uses top-level await, so esnext is required. Single CSS
  // file so the remote's styles load with it rather than as orphaned chunks.
  build: { target: "esnext", cssCodeSplit: false },
  server: {
    port: 5173,
    // The host fetches remoteEntry.js cross-origin during local dev
    // (VITE_MLSTUDIO_REMOTE=http://localhost:5173/assets/remoteEntry.js).
    cors: true,
    proxy: {
      "/api": withGatewayHeaders(BACKEND),
    },
  },
  preview: { port: 5173, cors: true },
});
