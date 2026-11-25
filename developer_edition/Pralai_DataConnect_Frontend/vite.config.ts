import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import federation from "@originjs/vite-plugin-federation";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const pkg = JSON.parse(readFileSync(fileURLToPath(new URL("./package.json", import.meta.url)), "utf-8"));

/*
 * DataConnect — Module Federation REMOTE (lives in the customer data plane).
 *
 * The Control Portal (host) loads this via a same-origin relative URL that is
 * proxied through the BFF → broker → tunnel → Envoy → this container. The host
 * imports the exposed "./DataConnectApp" and mounts it inside its own shell —
 * one page, one React runtime (shared singleton), no iframe.
 *
 * Build flags:
 *  - target: esnext / cssCodeSplit: false — required for federation to emit
 *    a working remoteEntry.js with its CSS inlined.
 *  - minify was previously disabled, carried over from a different project's
 *    Module Federation troubleshooting ("proven product-mfe reference") and
 *    never actually re-verified against this plugin/version. It isn't
 *    needed: tests/e2e/federation-mount.spec.ts and
 *    federation-host-contract.spec.ts build for real and exercise a
 *    synthetic host mounting this remote (shared-scope negotiation, CSS
 *    injection, cross-mount isolation, ...), and all pass minified — cutting
 *    the exposed chunk from ~390KB to ~183KB (raw) in the process. Leave
 *    minify at Vite's default (esbuild) unless a future federation-plugin
 *    upgrade regresses one of those tests, in which case that failure is the
 *    evidence, not a comment claiming a flag is required without it.
 *  - sourcemap: "hidden" — maps are emitted (for pulling out of a build
 *    artifact when investigating a production error) but bundles carry no
 *    `//# sourceMappingURL` comment, so browsers/devtools never fetch them
 *    automatically. server.ts additionally refuses to serve *.map at all
 *    (see its 404 guard) — "hidden" alone still leaves the file sitting at a
 *    guessable `<chunk>.js.map` path next to a chunk name that's public by
 *    construction, so the server-side block is what actually keeps a
 *    de-minified source reconstruction from being one request away.
 */
export default defineConfig({
  // Route use-sync-external-store (CJS, used by react-redux) to ESM files that import the
  // host-shared React. Without this the remote bundles a private React copy and crashes
  // with "Cannot read properties of null (reading 'useRef')" inside the Control Portal.
  resolve: {
    alias: [
      { find: /^use-sync-external-store\/(shim\/)?with-selector(\.js)?$/, replacement: fileURLToPath(new URL("./src/federation/useSyncExternalStoreWithSelector.ts", import.meta.url)) },
      { find: /^use-sync-external-store\/shim(\/index)?(\.js)?$/, replacement: fileURLToPath(new URL("./src/federation/useSyncExternalStoreShim.ts", import.meta.url)) },
    ],
  },
  plugins: [
    react(),
    federation({
      name: "dataconnect_mfe",
      filename: "remoteEntry.js",
      exposes: {
        "./DataConnectApp": "./src/DataConnectApp.tsx",
      },
      // Declared as an object (not the bare-name array shorthand) so each
      // entry carries requiredVersion: without it, @originjs/vite-plugin-
      // federation's runtime accepts whatever React version the host's
      // shared scope happens to offer, unchecked — an incompatible host
      // build would get silently wired into this remote's component tree
      // instead of being rejected in favor of this remote's own bundled
      // copy.
      shared: {
        react: { requiredVersion: pkg.dependencies.react },
        "react-dom": { requiredVersion: pkg.dependencies["react-dom"] },
      },
    }),
  ],
  build: {
    target: "esnext",
    cssCodeSplit: false,
    sourcemap: "hidden",
  },
  server: {
    port: 5001,
    cors: true,
  },
  preview: {
    port: 5001,
    cors: true,
  },
});
