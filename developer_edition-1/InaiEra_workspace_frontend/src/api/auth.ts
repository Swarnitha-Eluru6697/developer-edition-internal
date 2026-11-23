/**
 * Auth slice — establishes a workspace session (WS-AUTH-010).
 *
 * Control plane hands off to the workspace like this:
 *
 *   the control plane (e.g. www.app.inaiera.com; runtime-configured)  (user logs in)
 *     └─ redirect to orgid.app.workspace.inaiera.com/#token=<jwt>
 *         └─ establishSessionFromUrl() exchanges that one-time JWT for an
 *            HttpOnly, Secure, SameSite session cookie via
 *            POST /api/v1/auth/exchange (see app/api/auth.py), then strips
 *            the token from the URL. The token travels in the URL *fragment*
 *            only; a `?token=` query string is never accepted (W-005). From then on the cookie authenticates
 *            every same-origin request automatically — the JWT is never
 *            held in JS-readable storage (no localStorage, no persisted
 *            Redux state), so an XSS payload can't read it out of either.
 *
 * Refresh is delegated to the control plane: when /auth/me returns 401, the
 * SPA bounces the user back to the control plane's login URL (runtime config) which mints a fresh
 * token and redirects back through this same exchange.
 */
import { createSlice } from "@reduxjs/toolkit";
import { apiUrl, isEmbedded } from "./apiBase";
import { controlPlaneLoginUrl } from "./runtimeConfig";

interface AuthState {
  // Not the JWT — just "did establishSessionFromUrl() run and succeed this
  // page load", so the UI knows whether it's worth even trying /auth/me yet.
  sessionEstablished: boolean;
}

const initialState: AuthState = {
  sessionEstablished: false,
};

export const authSlice = createSlice({
  name: "auth",
  initialState,
  reducers: {
    sessionEstablished(state) {
      state.sessionEstablished = true;
    },
    sessionCleared(state) {
      state.sessionEstablished = false;
    },
  },
});

export const { sessionEstablished, sessionCleared } = authSlice.actions;
export const selectSessionEstablished = (state: { auth: AuthState }) => state.auth.sessionEstablished;
export default authSlice.reducer;

/**
 * Call once on app boot, before the first render. If the URL carries a
 * one-time `#token=` fragment from the control plane, exchange it for a
 * session cookie and strip it from the URL/history immediately (so it never
 * survives in browser history or a screenshot) — this must complete before
 * any authenticated API call fires, since the cookie doesn't exist until the
 * exchange responds.
 *
 * A `?token=` in the query string is NOT a handoff: the browser has already
 * sent that URL to the server (and through every proxy and log in between)
 * before any JS runs, so the credential is burned. It is scrubbed from the
 * address bar and ignored — never exchanged (W-005).
 *
 * If there's no token in the URL, there's nothing to exchange; whether an
 * existing cookie session is still valid is what /auth/me (App.tsx, on
 * load) actually confirms.
 */
export async function establishSessionFromUrl(): Promise<boolean> {
  if (typeof window === "undefined") return false;
  // Embedded in the portal there is no handoff: the portal's session already authenticates every call.
  if (isEmbedded()) return false;
  const url = new URL(window.location.href);
  const before = url.toString();
  const token = takeHandoffToken(url);
  if (url.toString() !== before) window.history.replaceState({}, "", url.toString());
  if (!token) return false;

  try {
    const resp = await fetch(apiUrl("/api/v1/auth/exchange"), {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
    });
    return resp.ok;
  } catch {
    return false;
  }
}

/**
 * The one-time handoff token, removed from `url`. The only accepted carrier
 * is the fragment (`#token=`): browsers never send it to any server, so it
 * can't land in access logs, proxy logs, analytics or a Referer header
 * (WS-AUTH-010). A `?token=` query parameter is removed from `url` but never
 * returned: it has already been transmitted by the time this runs, so
 * honouring it would only encourage control planes to keep sending it (W-005).
 */
export function takeHandoffToken(url: URL): string | null {
  const fragment = new URLSearchParams(url.hash.replace(/^#/, ""));
  const fromFragment = fragment.get("token");
  if (fromFragment) {
    fragment.delete("token");
    const rest = fragment.toString();
    url.hash = rest ? `#${rest}` : "";
  }
  url.searchParams.delete("token");     // scrub only — never used
  url.searchParams.delete("handoff");   // dev redirect marker (vite.config.docker.ts)
  return fromFragment || null;
}

/**
 * Clear the session. The cookie is HttpOnly, so JS cannot delete it
 * directly — this has to be a server round-trip.
 */
export async function logout(): Promise<void> {
  try {
    await fetch(apiUrl("/api/v1/auth/logout"), { method: "POST", credentials: "same-origin" });
  } catch {
    /* best-effort — the caller reloads regardless, landing back on /auth/me */
  }
}

/**
 * Redirect to the control plane to refresh credentials. Called when an API
 * call returns 401.
 */
export function redirectToControlPlaneLogin(): void {
  // The control plane's URL is runtime configuration (W-006), not a constant: see api/runtimeConfig.ts.
  window.location.href = controlPlaneLoginUrl(window.location.href);
}
