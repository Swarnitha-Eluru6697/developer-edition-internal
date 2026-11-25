/**
 * Shown instead of the app when the caller may not open Workspace: GET /api/v1/me did not list `workspace:access:view` (or could
 * not be loaded — unknown permissions are no permissions). Access is granted in the Admin Portal; nothing here can change it.
 */
import { apiErrorStatus } from "../api/errors";

export function NoAccessScreen({ error }: { error?: unknown }) {
  const status = error ? apiErrorStatus(error) : undefined;
  return (
    <div role="alert" style={{ height: "100vh", boxSizing: "border-box", overflow: "auto" }}>
      <div style={{ padding: 32, fontFamily: "system-ui, sans-serif", maxWidth: 640, color: "var(--ws-text, #14201B)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 17, fontWeight: 600, marginBottom: 10 }}>
          <i className="ti ti-lock" style={{ color: "var(--ws-accent, #086347)", fontSize: 20 }} />
          You don't have access to Workspace
        </div>
        <div style={{ fontSize: 13.5, color: "var(--ws-text-2, #4b5a53)", lineHeight: 1.6 }}>
          {error
            ? <>Your Workspace permissions could not be loaded{status ? ` (HTTP ${status})` : ""}, so nothing is shown. Try again in a moment.</>
            : <>Your account is signed in, but it has not been given permission to open Workspace. Ask your administrator to grant it in the Admin Portal; it takes effect at your next sign-in.</>}
        </div>
      </div>
    </div>
  );
}
