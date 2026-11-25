/**
 * Shown when GET /auth/me fails. It used to sit on "Authenticating…" forever
 * (or bounce to a dead control-plane URL); this names the actual failure so
 * it's fixable.
 */
import { logout } from "../api/auth";
import { apiErrorMessage, apiErrorStatus } from "../api/errors";
import { isLocalDev } from "./env";

export function AuthErrorScreen({ error }: { error: unknown }) {
  const status = apiErrorStatus(error);
  const detail = apiErrorMessage(error, "", { prefer: "detail" }) || null;
  const unreachable = status === "FETCH_ERROR" || status === null;
  return (
    <div style={{ height: "100vh", boxSizing: "border-box", overflow: "auto" }}>
    <div style={{ padding: 32, fontFamily: "system-ui, sans-serif", maxWidth: 640, color: "var(--ws-text, #14201B)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 17, fontWeight: 600, marginBottom: 10 }}>
        <i className="ti ti-alert-circle" style={{ color: "var(--ws-bad, #b23b3b)", fontSize: 20 }} />
        {unreachable ? "Can't reach the workspace API" : "Not signed in"}
      </div>
      <div style={{ fontSize: 13.5, color: "var(--ws-text-2, #4b5a53)", lineHeight: 1.6, marginBottom: 14 }}>
        {unreachable
          ? <>The request to <code>/api/v1/auth/me</code> failed. The workspace service is probably down — check its container logs.</>
          : <>The workspace service rejected this token{status ? ` (HTTP ${status})` : ""}.</>}
        {detail && <div style={{ marginTop: 8, fontFamily: "monospace", fontSize: 12, background: "var(--ws-bad-bg, #fbe9e9)", border: "0.5px solid var(--ws-bad-line, #fecaca)", borderRadius: 6, padding: "8px 10px", color: "var(--ws-bad, #b23b3b)" }}>{detail}</div>}
      </div>
      {/* The session lives in an HttpOnly cookie now (WS-AUTH-010) — a bad
          one survives reloads and pins you to this screen until the
          server clears it, which is why this has to be a network call
          rather than just clearing local state. */}
      <button
        onClick={() => { logout().finally(() => window.location.reload()); }}
        style={{ height: 32, padding: "0 16px", fontSize: 13, background: "var(--ws-primary, #0A7D5A)", color: "var(--ws-on-primary, #fff)", border: "none", borderRadius: 6, cursor: "pointer", marginBottom: 16 }}
      >
        Clear session &amp; reload
      </button>
      {isLocalDev() && (
        <div style={{ fontSize: 13, color: "var(--ws-text-2, #4b5a53)", lineHeight: 1.7, background: "var(--ws-bg, #F8FAF9)", border: "0.5px solid var(--ws-line, #E3E8E2)", borderRadius: 8, padding: "12px 14px" }}>
          <div style={{ fontWeight: 600, marginBottom: 6, color: "var(--ws-text, #14201B)" }}>Local dev</div>
          <code>request did not come through the gateway</code> means the request skipped the Vite
          dev gateway (dev-gateway.ts), or its secret no longer matches the backend's
          ENVOY_INTERNAL_SECRET — restart the web container after changing either.{" "}
          <code>UNKNOWN_KID</code> means the token was signed by a key the JWKS stub currently on
          :9999 doesn't serve — usually because a different project's stub grabbed the port.
          Check who owns it, then reopen with a token that stub signed:
          <div style={{ fontFamily: "monospace", fontSize: 12, marginTop: 8, background: "var(--ws-surface, #fff)", border: "0.5px solid var(--ws-line, #E3E8E2)", borderRadius: 6, padding: "8px 10px", overflowX: "auto", whiteSpace: "pre" }}>
{`curl -s localhost:9999/.well-known/jwks.json   # which kid is live?

# the workspace's own stub (kid dev-key-1):
open "http://localhost:5179/#token=$(cat ~/.inaiera-dev-jwks/token.txt)"`}
          </div>
        </div>
      )}
    </div>
    </div>
  );
}
