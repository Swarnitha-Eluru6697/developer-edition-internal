/**
 * GitModal — "Create workspace from Git repository"
 * Matches the Snowflake pattern exactly: single-screen modal with
 * provider selection, repo URL, workspace name, auth method cards,
 * and a single Create button.
 *
 * Calls POST /git/workspaces/from-git (atomic create + bind).
 * Auth status is real (GET /git/connections): OAuth is offered only when this
 * deployment has an OAuth app for the provider; "Connect" fetches the
 * authorize_url from GET /git/{provider}/connect and redirects; the return
 * (/?git_connected=…) is handled by useGitOAuthReturn, which reopens this
 * dialog with the form restored. With OAuth (or a saved PAT) the backend
 * clones using the stored credential.
 */
import { useEffect, useState } from "react";
import { useDispatch } from "react-redux";
import {
  useCreateWorkspaceFromGitMutation,
  useDisconnectGitMutation,
  useLazyInitiateOAuthQuery,
  useListGitConnectionsQuery,
  useStorePATMutation,
} from "../api";
import { askConfirm } from "../../../shared/ui/dialogStore";
import { apiErrorMessage } from "../../../api/errors";
import { closeGitModal, setPendingWorkspace, notify } from "../../../store/uiSlice";
import { Drawer } from "../../../shared/ui/Drawer";
import { drawerBtn } from "../../../shared/ui/drawerStyles";
import type { AppDispatch } from "../../../store";

import { GitProviderBadge } from "./GitProviderBadge";

const PROVIDERS = [
  { id: "github", label: "GitHub" },
  { id: "gitlab", label: "GitLab" },
  { id: "bitbucket", label: "Bitbucket" },
  { id: "azure_devops", label: "Azure DevOps" },
] as const;

type Provider = (typeof PROVIDERS)[number]["id"];
type AuthMethod = "oauth" | "pat" | "public";

const PROVIDER_URLS: Record<Provider, string> = {
  github: "https://github.com/",
  gitlab: "https://gitlab.com/",
  bitbucket: "https://bitbucket.org/",
  azure_devops: "https://dev.azure.com/",
};

// The form survives the OAuth round trip (the browser leaves for the provider
// and comes back to /?git_connected=…), so the user doesn't retype it.
const DRAFT_KEY = "gitModal.draft.v1";
interface Draft { provider: Provider; repoUrl: string; wsName: string; branch: string }
function loadDraft(): Partial<Draft> {
  try { return JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "{}"); } catch { return {}; }
}

/** Repo name from a URL, e.g. ".../acme/marketing-analytics.git" -> "marketing-analytics". */
function repoNameFromUrl(url: string): string {
  const m = url.trim().replace(/\/+$/, "").match(/([^/]+?)(?:\.git)?$/);
  return m ? m[1] : "";
}

export function GitModal() {
  const dispatch = useDispatch<AppDispatch>();
  const [draft] = useState(loadDraft);
  const [provider, setProvider] = useState<Provider>(draft.provider ?? "github");
  const [repoUrl, setRepoUrl] = useState(draft.repoUrl ?? "");
  const [wsName, setWsName] = useState(draft.wsName ?? "");
  const [nameEdited, setNameEdited] = useState(!!draft.wsName);
  const [branch, setBranch] = useState(draft.branch ?? "");
  const [authMethod, setAuthMethod] = useState<AuthMethod | null>(null);
  const [pat, setPat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [createFromGit] = useCreateWorkspaceFromGitMutation();
  const [storePAT] = useStorePATMutation();
  const { data: connections, isLoading: loadingConnections, refetch: refetchConnections } =
    useListGitConnectionsQuery(undefined, { refetchOnMountOrArgChange: true });
  const [startOAuth, { isFetching: connecting }] = useLazyInitiateOAuthQuery();
  const [disconnectGit, { isLoading: disconnecting }] = useDisconnectGitMutation();

  const conn = connections?.find((c) => c.provider === provider);
  const providerLabel = PROVIDERS.find((p) => p.id === provider)?.label ?? provider;
  const hasCredential = !!conn?.connected && !conn.expired;

  // Default the auth method from the REAL status once it's known: OAuth when
  // this deployment has an OAuth app, else a saved credential (PAT), else public.
  useEffect(() => {
    if (!connections || authMethod !== null) return;
    setAuthMethod(conn?.oauth_available ? "oauth" : conn?.connected ? "pat" : "public");
  }, [connections, conn, authMethod]);

  // Never leave OAuth selected for a provider without an OAuth app.
  useEffect(() => {
    if (authMethod === "oauth" && conn && !conn.oauth_available) setAuthMethod(conn.connected ? "pat" : "public");
  }, [conn, authMethod]);

  // Workspace name follows the repo name until the user types their own.
  useEffect(() => {
    if (!nameEdited) setWsName(repoNameFromUrl(repoUrl));
  }, [repoUrl, nameEdited]);

  const handleConnect = async () => {
    setError(null);
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ provider, repoUrl, wsName: nameEdited ? wsName : "", branch }));
    } catch { /* storage unavailable: the form just won't be restored */ }
    try {
      const { authorize_url } = await startOAuth(provider).unwrap();
      window.location.href = authorize_url;   // provider -> /api/v1/git/{p}/callback -> /?git_connected=
    } catch (e) {
      setError(apiErrorMessage(e, `Couldn't start sign-in with ${providerLabel}.`));
    }
  };

  const handleDisconnect = async () => {
    if (!(await askConfirm({
      title: `Disconnect ${providerLabel}`,
      message: `Remove your saved ${providerLabel} credential? Workspaces using it can't push or pull until you connect again.`,
      confirmLabel: "Disconnect", danger: true,
    }))) return;
    try {
      await disconnectGit(provider).unwrap();
      await refetchConnections();
      dispatch(notify({ kind: "success", message: `Disconnected ${providerLabel}` }));
    } catch (e) {
      setError(apiErrorMessage(e, `Couldn't disconnect ${providerLabel}.`));
    }
  };

  const handleProviderChange = (p: Provider) => {
    setProvider(p);
    const base = PROVIDER_URLS[p];
    if (!repoUrl.startsWith(PROVIDER_URLS[provider])) return;
    setRepoUrl(base + repoUrl.slice(PROVIDER_URLS[provider].length));
  };

  const handleCreate = async () => {
    if (!repoUrl.trim() || !wsName.trim()) {
      setError("Repository URL and workspace name are required.");
      return;
    }
    if (authMethod === "oauth" && !hasCredential) {
      setError(`Connect ${providerLabel} first (or choose another authentication method).`);
      return;
    }
    if (authMethod === "pat" && !pat.trim() && !hasCredential) {
      setError("Paste a personal access token with repo access.");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const result = await createFromGit({
        provider,
        repo_url: repoUrl.trim(),
        workspace_name: wsName.trim(),
        // W-014: empty = the repository's default branch; a branch typed here is a requirement (never silently replaced).
        default_branch: branch.trim() || undefined,
        // Send the PAT so the backend can clone a private repo and import files.
        access_token: authMethod === "pat" && pat.trim() ? pat.trim() : undefined,
      }).unwrap();

      // OAuth / saved credential: the backend clones with the stored token.
      if (authMethod === "pat" && pat.trim()) {
        await storePAT({ workspaceId: result.workspace_id, token: pat.trim() }).unwrap();
      }
      try { sessionStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
      // Notify success, reporting how many files were imported (or why not).
      if (result.import_error) {
        dispatch(notify({
          kind: "info",
          message: `Workspace "${result.workspace_name}" created, but files couldn't be pulled: ${result.import_error}`,
        }));
      } else {
        dispatch(notify({
          kind: "success",
          message: `Workspace "${result.workspace_name}" created — ${result.files_imported ?? 0} file(s) imported from the repo.`,
        }));
      }
      // Switch the shell to the workspace we just created so the user sees it.
      dispatch(setPendingWorkspace(result.workspace_id));
      dispatch(closeGitModal());
    } catch (e) {
      const msg = apiErrorMessage(e, "Failed to create workspace.");
      setError(msg);
      dispatch(notify({ kind: "error", message: msg }));
    } finally {
      setCreating(false);
    }
  };

  const close = () => dispatch(closeGitModal());

  // Right-side slide-over like every other Workspace dialog (shared Drawer:
  // Esc / backdrop close, disabled while a create is in flight).
  return (
    <Drawer
      title="Create workspace from Git repository"
      icon="ti-git-fork"
      onClose={close}
      busy={creating}
      width={480}
      footer={<>
        <button onClick={handleCreate} disabled={creating} style={drawerBtn(true)}>
          {creating ? "Creating…" : "Create"}
        </button>
        <button onClick={close} disabled={creating} style={drawerBtn(false)}>Cancel</button>
      </>}
    >
        <div style={{ fontSize: 12, color: "#888", marginBottom: 16 }}>
          Connect a provider to pull, edit, and push your project
        </div>

        {/* Provider selection */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 7 }}>Git provider</div>
          <div style={{ display: "flex", gap: 8 }}>
            {PROVIDERS.map((p) => (
              <div
                key={p.id}
                onClick={() => handleProviderChange(p.id)}
                style={{
                  flex: 1, border: `1px solid ${provider === p.id ? "#F97316" : "#E5E7EB"}`,
                  boxShadow: provider === p.id ? "0 0 0 3px #FFF7ED" : "none",
                  background: provider === p.id ? "#FFF7ED" : "#fff",
                  borderRadius: 11, padding: "12px 8px", cursor: "pointer", textAlign: "center",
                }}
              >
                <div style={{ display: "flex", justifyContent: "center", marginBottom: 6 }}>
                  <GitProviderBadge provider={p.id} size={34} />
                </div>
                <div style={{ fontSize: 12, fontWeight: 600, color: "#1F2937" }}>{p.label}</div>
              </div>
            ))}
          </div>
        </div>

        {/* Repo URL */}
        <div style={{ marginBottom: 12 }}>
          <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 5 }}>Repository URL</label>
          <input
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder={`${PROVIDER_URLS[provider]}your-org/your-repo`}
            style={{ width: "100%", height: 34, fontSize: 12, border: "0.5px solid #ccc", borderRadius: 6, padding: "0 9px", fontFamily: "monospace" }}
          />
        </div>

        {/* Workspace name */}
        <div style={{ marginBottom: 12 }}>
          <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 5 }}>Workspace name</label>
          <input
            value={wsName}
            onChange={(e) => { setWsName(e.target.value); setNameEdited(true); }}
            placeholder="defaults to the repository name"
            style={{ width: "100%", height: 34, fontSize: 12, border: "0.5px solid #ccc", borderRadius: 6, padding: "0 9px" }}
          />
        </div>

        {/* Branch */}
        <div style={{ marginBottom: 14 }}>
          <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 5 }}>Branch or tag <span style={{ fontWeight: 400, color: "#9ca3af" }}>(optional — empty imports the repository's default branch)</span></label>
          <input
            placeholder="repository default"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            style={{ width: "100%", height: 34, fontSize: 12, border: "0.5px solid #ccc", borderRadius: 6, padding: "0 9px", fontFamily: "monospace" }}
          />
        </div>

        {/* Auth method */}
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>Authentication method</div>
          <div style={{ display: "flex", gap: 7, marginBottom: 10 }}>
            {(["oauth", "pat", "public"] as AuthMethod[]).map((m) => {
              const disabled = m === "oauth" && !!conn && !conn.oauth_available;
              const selected = authMethod === m;
              return (
                <div
                  key={m}
                  onClick={() => !disabled && setAuthMethod(m)}
                  title={disabled ? `OAuth isn't configured for ${providerLabel} on this deployment` : undefined}
                  style={{
                    flex: 1, border: `1.5px solid ${selected ? "#F97316" : "#ddd"}`,
                    background: selected ? "#FFF7ED" : disabled ? "#f6f6f4" : "#fff",
                    borderRadius: 8, padding: "9px 10px", cursor: disabled ? "not-allowed" : "pointer",
                    opacity: disabled ? 0.55 : 1,
                  }}
                >
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 3 }}>
                    {m === "oauth" ? "OAuth 2.0" : m === "pat" ? "Personal access token" : "Public repository"}
                    {selected && <i className="ti ti-check" style={{ color: "#F97316", marginLeft: 4 }} />}
                  </div>
                  <div style={{ fontSize: 11, color: "#666", lineHeight: 1.4 }}>
                    {m === "oauth" ? (disabled ? "Not configured on this deployment" : `Sign in to ${providerLabel}`)
                      : m === "pat" ? "Paste a token with repo access" : "No authentication needed"}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Real connection status for the selected provider (GET /git/connections). */}
          {authMethod !== "public" && (
            loadingConnections ? (
              <div style={{ fontSize: 12, color: "#888", padding: "6px 2px" }}>Checking your {providerLabel} connection…</div>
            ) : conn?.connected ? (
              <div style={{
                display: "flex", alignItems: "center", gap: 8, padding: "8px 10px", borderRadius: 7, marginBottom: 8,
                background: conn.expired ? "#fffbeb" : "#f0f9f4",
                border: `0.5px solid ${conn.expired ? "#fcd34d" : "#86efac"}`,
              }}>
                <i className={`ti ${conn.expired ? "ti-alert-triangle" : "ti-check"}`}
                   style={{ color: conn.expired ? "#b45309" : "#15803d", fontSize: 14 }} />
                <span style={{ fontSize: 12, color: conn.expired ? "#b45309" : "#15803d", fontWeight: 600 }}>
                  {conn.expired ? `${providerLabel} credential expired` : `Connected to ${providerLabel}`}
                </span>
                <span style={{ marginLeft: "auto", fontSize: 11, color: "#6b7280", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {conn.username ?? "saved credential"}
                </span>
                {conn.oauth_available && (
                  <button onClick={handleConnect} disabled={connecting}
                    style={{ background: "none", border: "none", padding: 0, fontSize: 11, color: "#C2410C", cursor: "pointer", textDecoration: "underline" }}>
                    {connecting ? "Redirecting…" : "Reconnect"}
                  </button>
                )}
                <button onClick={handleDisconnect} disabled={disconnecting}
                  style={{ background: "none", border: "none", padding: 0, fontSize: 11, color: "#dc2626", cursor: "pointer", textDecoration: "underline" }}>
                  {disconnecting ? "Disconnecting…" : "Disconnect"}
                </button>
              </div>
            ) : authMethod === "oauth" ? (
              <button onClick={handleConnect} disabled={connecting}
                style={{ ...drawerBtn(true), width: "100%", marginBottom: 8 }}>
                <i className="ti ti-login" style={{ fontSize: 14 }} />
                {connecting ? "Redirecting…" : `Connect ${providerLabel}`}
              </button>
            ) : (
              <div style={{ fontSize: 11.5, color: "#6b7280", marginBottom: 8 }}>
                No saved {providerLabel} credential yet — the token you paste is saved for push/pull.
              </div>
            )
          )}

          {authMethod === "pat" && (
            <input
              type="password"
              value={pat}
              onChange={(e) => setPat(e.target.value)}
              placeholder={hasCredential ? "Leave blank to use your saved credential" : "Paste a token with repo access"}
              style={{ width: "100%", height: 34, fontSize: 12, border: "0.5px solid #ccc", borderRadius: 6, padding: "0 9px", fontFamily: "monospace" }}
            />
          )}
        </div>

        {error && <div style={{ fontSize: 12, color: "#dc2626", marginBottom: 12, padding: "7px 10px", background: "#fee2e2", borderRadius: 6 }}>{error}</div>}
    </Drawer>
  );
}
