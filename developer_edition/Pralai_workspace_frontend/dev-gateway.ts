// Dev-only stand-in for Envoy in front of the workspace API.
//
// The backend no longer validates JWTs itself: it trusts identity headers that
// Envoy injects (x-user-id / x-user-name / x-user-groups / x-user-permissions / x-tenant-id) and
// only after checking the shared X-Inaiera-Gateway-Secret proves the request
// came through Envoy (app/core/deps.py). Locally there is no Envoy, so without
// this every /api call dies with 401 "request did not come through the gateway".
//
// This plugin does Envoy's job on the Vite dev server, the same way the UCS
// frontend's nginx does: verify the caller's JWT (Authorization header, or the
// workspace_session cookie set by /auth/exchange) against the JWKS stub, strip
// any client-supplied identity headers, then inject Envoy's headers plus the
// secret. It runs before Vite's proxy, so the proxy forwards what we set.
//
// Permissions: Envoy copies the ticket's `permissions` claim into x-user-permissions. The dev JWKS stub's token has no such
// claim, so locally the caller gets EVERY Workspace permission in openapi/permissions.json (a token that does carry
// `permissions` gets exactly those). x-user-roles is set from the ticket's `roles` claim, like Envoy sets it from the realm roles: it is
// display / audit identity only (the backend authorises on permissions only), but Workspace passes it on so the Data Quality service,
// which still derives its own dq.* permissions from the caller's roles, can tell who is calling. Without it local DQ calls are 403.
//
// apply: "serve" — it never runs in `vite build`; prod sits behind real Envoy.
import { readFileSync } from "node:fs";
import { createPublicKey, verify as verifySig, type JsonWebKey } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";

export interface DevGatewayOptions {
  /** Must equal ENVOY_INTERNAL_SECRET in the backend's docker-compose.yml. */
  secret: string;
  jwksUrl: string;
  issuer: string;
  audience: string;
}

const SESSION_COOKIE = "workspace_session";
const IDENTITY_HEADERS = [
  "x-user-id", "x-user-name", "x-user-groups", "x-user-roles", "x-user-role", "x-user-permissions", "x-tenant-id", "x-inaiera-gateway-secret",
];
const JWKS_TTL_MS = 60_000;

class GatewayError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

const b64url = (s: string) => Buffer.from(s, "base64url");

function extractToken(req: IncomingMessage): string | null {
  const auth = req.headers.authorization ?? "";
  if (auth.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim() || null;
  for (const part of (req.headers.cookie ?? "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === SESSION_COOKIE) return decodeURIComponent(v.join("=")) || null;
  }
  return null;
}

// Envoy's claim_to_headers renders list claims as base64(JSON); the backend's
// split_multi decodes that, so emit the same shape.
const listHeader = (v: unknown) => (Array.isArray(v) && v.length ? Buffer.from(JSON.stringify(v.map(String))).toString("base64") : null);

/** Every permission ID in openapi/permissions.json (read when the dev server starts). */
function allPermissions(): string[] {
  const doc = JSON.parse(readFileSync(new URL("./openapi/permissions.json", import.meta.url), "utf8")) as { permissions: { id: string }[] };
  return doc.permissions.map((p) => p.id);
}

export function devGateway(opts: DevGatewayOptions): Plugin {
  let devPermissions: string[] | null = null;
  let jwks: { keys: (JsonWebKey & { kid?: string })[]; at: number } | null = null;

  async function loadKeys(force: boolean) {
    if (!force && jwks && Date.now() - jwks.at < JWKS_TTL_MS) return jwks.keys;
    let res: Response;
    try {
      res = await fetch(opts.jwksUrl);
    } catch (e) {
      throw new GatewayError("JWKS_UNREACHABLE", `dev gateway can't reach JWKS at ${opts.jwksUrl} — is the jwks stub running?`);
    }
    if (!res.ok) throw new GatewayError("JWKS_UNREACHABLE", `JWKS at ${opts.jwksUrl} returned HTTP ${res.status}`);
    jwks = { keys: ((await res.json()) as { keys: [] }).keys ?? [], at: Date.now() };
    return jwks.keys;
  }

  async function verifyJwt(token: string): Promise<Record<string, any>> {
    const parts = token.split(".");
    if (parts.length !== 3) throw new GatewayError("MALFORMED_TOKEN", "token is not a JWT");
    let header: { alg?: string; kid?: string };
    try {
      header = JSON.parse(b64url(parts[0]).toString("utf8"));
    } catch {
      throw new GatewayError("MALFORMED_TOKEN", "token header is not valid JSON");
    }
    if (header.alg !== "RS256") throw new GatewayError("UNSUPPORTED_ALG", `unsupported alg ${header.alg}`);

    // Refetch once on a miss: the stub may have restarted with a new key.
    let jwk = (await loadKeys(false)).find((k) => k.kid === header.kid);
    if (!jwk) jwk = (await loadKeys(true)).find((k) => k.kid === header.kid);
    if (!jwk) throw new GatewayError("UNKNOWN_KID", `token kid "${header.kid}" is not served by ${opts.jwksUrl}`);

    const ok = verifySig("RSA-SHA256", Buffer.from(`${parts[0]}.${parts[1]}`), createPublicKey({ key: jwk, format: "jwk" }), b64url(parts[2]));
    if (!ok) throw new GatewayError("BAD_SIGNATURE", "token signature is invalid");

    const claims = JSON.parse(b64url(parts[1]).toString("utf8"));
    const now = Math.floor(Date.now() / 1000);
    if (typeof claims.exp === "number" && claims.exp < now - 30) throw new GatewayError("TOKEN_EXPIRED", "token has expired");
    if (claims.iss !== opts.issuer) throw new GatewayError("BAD_ISSUER", `token iss "${claims.iss}" != "${opts.issuer}"`);
    const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!aud.includes(opts.audience)) throw new GatewayError("BAD_AUDIENCE", `token aud does not include "${opts.audience}"`);
    return claims;
  }

  async function handle(req: IncomingMessage, res: ServerResponse, next: () => void) {
    if (!(req.url ?? "").startsWith("/api")) return next();

    // Never let the browser speak for the gateway.
    for (const h of IDENTITY_HEADERS) delete req.headers[h];

    // W-004: logout must work with an expired or malformed credential (that is when a stale cookie has to be cleared), so it passes
    // through WITHOUT identity headers when the token does not verify — like Envoy's allow_missing_or_failed for that one route.
    const isLogout = req.method === "POST" && (req.url ?? "").split("?")[0] === "/api/v1/auth/logout";
    const token = extractToken(req);
    // No credential: forward as-is, like Envoy's allow_missing, and let the
    // backend decide (public routes pass, protected ones 401).
    if (!token) return next();

    try {
      const c = await verifyJwt(token);
      // The Keycloak claims Envoy maps, falling back to the dev stub's
      // control-plane claims (sub / org_id). `sub` before `email`:
      // the backend keys users by this value, and existing local workspaces
      // were created under the stub token's `sub` — keying by email would
      // silently create a fresh, empty user instead.
      const name = c.preferred_username ?? c.sub ?? c.email;
      const tenant = c.tenant ?? c.org_id;
      const permissions = listHeader(Array.isArray(c.permissions) ? c.permissions : (devPermissions ??= allPermissions()));
      const groups = listHeader(c.groups);
      const roles = listHeader(c.roles);
      // X-003: the immutable control-plane subject is the user's identity everywhere; the name is only an attribute.
      if (c.sub) req.headers["x-user-id"] = String(c.sub);
      if (name) req.headers["x-user-name"] = String(name);
      if (tenant) req.headers["x-tenant-id"] = String(tenant);
      if (permissions) req.headers["x-user-permissions"] = permissions;
      if (groups) req.headers["x-user-groups"] = groups;
      if (roles) req.headers["x-user-roles"] = roles;
      req.headers["x-inaiera-gateway-secret"] = opts.secret;
      next();
    } catch (e) {
      if (isLogout) return next();
      const err = e instanceof GatewayError ? e : new GatewayError("INVALID_TOKEN", String((e as Error)?.message ?? e));
      res.statusCode = 401;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ detail: { code: err.code, message: `${err.code}: ${err.message}` } }));
    }
  }

  return {
    name: "inaiera-dev-gateway",
    apply: "serve",
    configureServer(server) {
      // Registered directly (not via a returned hook) so it runs before Vite's
      // built-in proxy middleware.
      server.middlewares.use((req, res, next) => {
        handle(req, res, next).catch(next);
      });
    },
  };
}
