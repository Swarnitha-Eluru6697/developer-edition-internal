/**
 * Runtime configuration (W-006): deployment-specific values are read when the page loads, not baked in at build time.
 *
 * The control-plane login URL used to be the literal `https://www.app.inaiera.com/login`, which tied staging, private-cloud,
 * region-specific and white-label deployments to the public production site. Now, in order of precedence:
 *   1. `GET /config.json` — served by the Workspace backend from its settings (CONTROL_PLANE_LOGIN_URL, else derived from its
 *      CONTROL_PLANE_ISSUER), or by whatever serves the SPA (public/config.json, replaceable per deployment without a rebuild);
 *   2. `VITE_CONTROL_PLANE_LOGIN_URL` at build time;
 *   3. the production default.
 * Only an absolute http(s) URL is accepted from either source; anything else is ignored (a config file must not be able to turn the
 * 401 redirect into `javascript:` or a relative path). A config that cannot be fetched never blocks the app.
 */
export const DEFAULT_CONTROL_PLANE_LOGIN_URL = "https://www.app.inaiera.com/login";

export interface RuntimeConfig {
  controlPlaneLoginUrl: string;
}

/** The value if it is an absolute http(s) URL, else null. */
export function sanitizeLoginUrl(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value.trim());
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

const buildTime = (): string => sanitizeLoginUrl(import.meta.env?.VITE_CONTROL_PLANE_LOGIN_URL) ?? DEFAULT_CONTROL_PLANE_LOGIN_URL;

let current: RuntimeConfig = { controlPlaneLoginUrl: buildTime() };

export function runtimeConfig(): RuntimeConfig {
  return current;
}

/** Fetch /config.json once at startup. Never throws and never waits longer than `timeoutMs`: the build-time value stays on any failure. */
export async function loadRuntimeConfig(fetchImpl: typeof fetch = fetch, timeoutMs = 3000): Promise<RuntimeConfig> {
  current = { controlPlaneLoginUrl: buildTime() };
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const res = await fetchImpl("/config.json", { cache: "no-store", credentials: "same-origin", signal: controller?.signal });
    if (res.ok) {
      const body: unknown = await res.json();
      const fromFile = sanitizeLoginUrl((body as { controlPlaneLoginUrl?: unknown } | null)?.controlPlaneLoginUrl);
      if (fromFile) current = { controlPlaneLoginUrl: fromFile };
    }
  } catch {
    /* no config served (or not JSON): keep the build-time value */
  } finally {
    if (timer) clearTimeout(timer);
  }
  return current;
}

/** Where to send the user to log in again, with `next` = the page to come back to. */
export function controlPlaneLoginUrl(next: string): string {
  const url = new URL(current.controlPlaneLoginUrl);
  url.searchParams.set("next", next);
  return url.toString();
}
