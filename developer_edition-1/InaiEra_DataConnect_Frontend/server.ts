/*
 * Static server for the built DataConnect remote (dist/), including
 * remoteEntry.js and its chunks.
 *
 * Host contract — who is allowed to load this remote cross-origin:
 * this server does NOT trust network placement (e.g. "only Envoy inside the
 * data plane can reach this port") as its access control, because that's a
 * network-layer assumption a CORS header can't see or verify — a proxy
 * misconfiguration, an accidentally published port, or a deployment that
 * puts this service somewhere more exposed all silently defeat it while a
 * wildcard `Access-Control-Allow-Origin: *` keeps right on trusting every
 * origin that ever reaches this port. Instead, the actual allow-list is a
 * config input the host deployment must supply:
 *
 *   DATACONNECT_CORS_ORIGINS — comma-separated list of origins allowed to
 *     fetch these assets cross-origin, e.g.
 *     "https://portal.example.com,http://localhost:3000" for a Control
 *     Portal host running on those origins. Use "*" only if every caller is
 *     genuinely untrusted-origin-safe (this serves public, non-secret JS
 *     bundles, so a wildcard isn't a data leak the way it would be for an
 *     API — but it does mean any site can fingerprint that this service is
 *     reachable, so prefer an explicit list). Same DATACONNECT_ prefix as
 *     the backend's own cors_origins setting (app/core/config.py there),
 *     but comma-separated here rather than that side's JSON-array string —
 *     this is a plain Node env var with no pydantic-settings-style parser,
 *     not a mismatch to "fix" into agreement.
 *
 * If unset, no Access-Control-Allow-Origin header is sent at all — the
 * federation host must then load this remote same-origin (e.g. through the
 * tunnel/reverse-proxy path described in README.md's "How it's hosted"),
 * which needs no CORS header in the first place since the browser never
 * treats it as cross-origin.
 */
import express, { Request, Response, NextFunction } from "express";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = process.env.PORT || 5001;

const ALLOWED_ORIGINS = (process.env.DATACONNECT_CORS_ORIGINS || "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);
const ALLOW_ALL_ORIGINS = ALLOWED_ORIGINS.includes("*");

if (ALLOWED_ORIGINS.length === 0) {
  console.log("[dataconnect-mfe] DATACONNECT_CORS_ORIGINS not set — serving same-origin only, no CORS header sent.");
} else if (ALLOW_ALL_ORIGINS) {
  console.warn("[dataconnect-mfe] DATACONNECT_CORS_ORIGINS=\"*\" — every origin may load these assets cross-origin.");
} else {
  console.log(`[dataconnect-mfe] Cross-origin loading allowed for: ${ALLOWED_ORIGINS.join(", ")}`);
}

app.use((req: Request, res: Response, next: NextFunction) => {
  const origin = req.headers.origin;
  if (ALLOW_ALL_ORIGINS) {
    res.setHeader("Access-Control-Allow-Origin", "*");
  } else if (origin && ALLOWED_ORIGINS.includes(origin)) {
    // Reflecting a specific allow-listed origin (rather than "*") plus
    // Vary: Origin is what actually makes this an allow-list instead of a
    // wildcard with extra steps — a cache sitting in front of this server
    // must not serve one origin's CORS-approved response to another origin.
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Vary", "Origin");
  }
  next();
});

// Source maps are built (vite.config.ts: sourcemap: "hidden") for pulling out of
// a build artifact when investigating a production error, not for public serving —
// "hidden" only omits the sourceMappingURL comment from the bundle; the .map file
// still sits right next to its chunk at a guessable path unless blocked here.
app.use((req: Request, res: Response, next: NextFunction) => {
  if (req.path.endsWith(".map")) { res.status(404).end("not found"); return; }
  next();
});

app.use(express.static(path.join(__dirname, "dist")));

app.listen(PORT, () => {
  console.log(`[dataconnect-mfe] serving built bundle on :${PORT}`);
});
