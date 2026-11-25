/**
 * Git "Changes" view for the file panel: repo/branch info, the changed-file
 * list, PAT entry, pull, commit-and-push and pull-request creation.
 */
import { useState } from "react";
import { useDispatch } from "react-redux";
import {
  useCommitAndPushMutation,
  useCreatePRMutation,
  useGetGitBindingQuery,
  useGetGitStatusQuery,
  usePullChangesMutation,
  usePushChangesMutation,
  useStorePATMutation,
} from "../api";
import { apiErrorMessage } from "../../../api/errors";
import { openGitModal, notify } from "../../../store/uiSlice";
import { useHasPermission } from "../../../rbac/hooks";
import type { AppDispatch } from "../../../store";
import type { UUID } from "../../../types";
import { repoShortName } from "../gitStatus";
import { openExternal } from "../../../shared/externalUrl";
import { GitProviderBadge } from "./GitProviderBadge";

export function GitChangesPanel({ workspaceId }: { workspaceId: UUID }) {
  const dispatch = useDispatch<AppDispatch>();
  const { data: binding } = useGetGitBindingQuery(workspaceId);
  const { data: gitStatus } = useGetGitStatusQuery(workspaceId);
  const [commitMsg, setCommitMsg] = useState("");
  const [commitAndPush] = useCommitAndPushMutation();
  const [pushChanges] = usePushChangesMutation();
  const [pullChanges] = usePullChangesMutation();
  const [storePAT] = useStorePATMutation();
  const [createPR] = useCreatePRMutation();
  const [patInput, setPatInput] = useState("");
  const [showPat, setShowPat] = useState(false);
  const [prTitle, setPrTitle] = useState("");
  const [showPRForm, setShowPRForm] = useState(false);
  // "Commit and push" is one combined action; workspace:git:write covers both.
  const canCommitAndPush = useHasPermission("workspace:git:write");

  const handleSetToken = async () => {
    if (!patInput.trim()) return;
    try {
      await storePAT({ workspaceId, token: patInput.trim() }).unwrap();
      dispatch(notify({ kind: "success", message: "Git token saved — you can push/pull now." }));
      setPatInput(""); setShowPat(false);
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Failed to save token") }));
    }
  };

  const handleCommit = async () => {
    if (!commitMsg.trim()) return;
    try {
      const res = await commitAndPush({ workspaceId, body: { message: commitMsg } }).unwrap();
      dispatch(notify({ kind: "success", message: `Committed ${res?.committed_file_count ?? ""} file(s) — pushing…` }));
      setCommitMsg("");
      try {
        await pushChanges(workspaceId).unwrap();
        dispatch(notify({ kind: "success", message: "Pushed to remote ✓" }));
      } catch (e) {
        dispatch(notify({ kind: "error", message: `Committed locally, but push failed: ${apiErrorMessage(e, "check credentials", { prefer: "detail" })}` }));
      }
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Commit failed") }));
    }
  };

  const handlePull = async () => {
    try {
      const res = await pullChanges(workspaceId).unwrap();
      dispatch(notify({ kind: "success", message: `Pulled — ${res?.files_synced ?? 0} file(s) synced` }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: `Pull failed: ${apiErrorMessage(e, "check credentials", { prefer: "detail" })}` }));
    }
  };

  const handlePR = async () => {
    if (!prTitle.trim() || !gitStatus?.branch) return;
    const result = await createPR({ workspaceId, body: { title: prTitle, source_branch: gitStatus.branch } }).unwrap();
    // The PR URL comes from the provider through the backend: open it only if it is https on the repository's own host, and without an
    // opener or referrer (W-023).
    let repoHost: string | undefined;
    try { repoHost = binding?.repo_url ? new URL(binding.repo_url).host : undefined; } catch { /* unparsable repo URL: no host to compare */ }
    if (!openExternal(result.url, { expectedHost: repoHost })) {
      dispatch(notify({ kind: "info", message: "Pull request created, but its link was not opened (it is not an https URL on the repository's host)." }));
    }
    setShowPRForm(false);
    setPrTitle("");
  };

  return (
    <div style={{ flex: 1, overflow: "auto", padding: 10 }}>
      {!binding ? (
        <div style={{ fontSize: 12, color: "#888", textAlign: "center", padding: 16 }}>
          No Git repository connected.{" "}
          <button onClick={() => dispatch(openGitModal())} style={{ background: "none", border: "none", color: "#086347", cursor: "pointer", textDecoration: "underline", fontSize: 12 }}>
            Connect now
          </button>
        </div>
      ) : (
        <>
          {/* Repo info */}
          <div style={{ border: "1.5px solid #B7E0CB", borderRadius: 8, padding: 10, background: "#E3F6EA", marginBottom: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 3 }}>
              <GitProviderBadge provider={binding.provider} size={20} />
              <span style={{ fontSize: 12, fontWeight: 600, color: "#14201B" }}>{repoShortName(binding.repo_url)}</span>
            </div>
            <div style={{ fontFamily: "monospace", fontSize: 11, color: "#5b6d62" }}>
              <i className="ti ti-git-branch" style={{ fontSize: 11 }} /> {gitStatus?.branch ?? binding.current_branch}
              {gitStatus && (gitStatus.ahead > 0 || gitStatus.behind > 0) && (
                <span style={{ marginLeft: 8 }}>
                  {gitStatus.ahead > 0 && <span style={{ color: "#2f6b14" }}>↑{gitStatus.ahead}</span>}
                  {gitStatus.behind > 0 && <span style={{ color: "#b23b3b", marginLeft: 4 }}>↓{gitStatus.behind}</span>}
                </span>
              )}
            </div>
          </div>

          {/* Changed files */}
          {gitStatus && (gitStatus.modified_files.length > 0 || gitStatus.untracked_files.length > 0) ? (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 7, color: "#2E3D36" }}>
                Changes ({gitStatus.modified_files.length + gitStatus.untracked_files.length})
              </div>
              {gitStatus.modified_files.map((f) => (
                <div key={f} style={{ display: "flex", alignItems: "center", gap: 7, padding: "5px 8px", borderRadius: 5, background: "#fff", border: "0.5px solid #e3e8e2", marginBottom: 4, fontFamily: "monospace", fontSize: 11.5 }}>
                  <i className="ti ti-file-diff" style={{ color: "#0A7D5A", fontSize: 12 }} />
                  <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f}</span>
                  <span style={{ color: "#a8470a", fontWeight: 700, fontSize: 11 }}>M</span>
                </div>
              ))}
              {gitStatus.untracked_files.map((f) => (
                <div key={f} style={{ display: "flex", alignItems: "center", gap: 7, padding: "5px 8px", borderRadius: 5, background: "#fff", border: "0.5px solid #e3e8e2", marginBottom: 4, fontFamily: "monospace", fontSize: 11.5 }}>
                  <i className="ti ti-file-plus" style={{ color: "#2f6b14", fontSize: 12 }} />
                  <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f}</span>
                  <span style={{ color: "#2f6b14", fontWeight: 700, fontSize: 11 }}>A</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 12, color: "#888", padding: "12px 0", textAlign: "center" }}>
              <i className="ti ti-check" style={{ fontSize: 18, display: "block", marginBottom: 5, color: "#2f6b14" }} />
              No uncommitted changes
            </div>
          )}

          {/* Git credentials (PAT) */}
          <button
            onClick={() => setShowPat(!showPat)}
            title="Set the personal access token used for push/pull"
            style={{ height: 26, fontSize: 11, width: "100%", marginBottom: 6, background: "#fff", color: "#5b6d62", border: "0.5px solid #cfd8d1", borderRadius: 6, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 5 }}
          ><i className="ti ti-key" style={{ fontSize: 12 }} />{showPat ? "Cancel" : "Set / update Git token"}</button>
          {showPat && (
            <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
              <input
                type="password"
                placeholder="ghp_… (needs repo write scope)"
                value={patInput}
                onChange={(e) => setPatInput(e.target.value)}
                style={{ flex: 1, fontSize: 11, height: 28, border: "0.5px solid #cfd8d1", borderRadius: 6, padding: "0 8px", fontFamily: "monospace" }}
              />
              <button onClick={handleSetToken} disabled={!patInput.trim()}
                style={{ height: 28, padding: "0 12px", fontSize: 11.5, background: "#0A7D5A", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", opacity: !patInput.trim() ? 0.5 : 1 }}>Save</button>
            </div>
          )}

          {/* Pull from remote */}
          <button
            onClick={handlePull}
            title="Fetch the latest from the remote branch and sync files"
            style={{ height: 30, fontSize: 12, width: "100%", marginBottom: 8, background: "#fff", color: "#2E3D36", border: "0.5px solid #cfd8d1", borderRadius: 6, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}
          ><i className="ti ti-arrow-down" style={{ fontSize: 13 }} />Pull from remote</button>

          {/* Commit form — needs workspace:git:write (this button commits and pushes). */}
          {canCommitAndPush ? (<>
            <textarea
              rows={2}
              placeholder="Commit message…"
              value={commitMsg}
              onChange={(e) => setCommitMsg(e.target.value)}
              style={{ fontSize: 12, width: "100%", resize: "none", padding: 7, borderRadius: 6, border: "0.5px solid #cfd8d1", marginBottom: 7 }}
            />
            <button
              onClick={handleCommit}
              disabled={!commitMsg.trim()}
              style={{ height: 30, fontSize: 12, width: "100%", marginBottom: 6, background: "#0A7D5A", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", opacity: !commitMsg.trim() ? 0.5 : 1 }}
            >Commit and push</button>
          </>) : (
            <div style={{ fontSize: 11, color: "#73827a", padding: "6px 2px 10px" }}>
              You don't have permission to commit or push to Git.
            </div>
          )}
          <button
            onClick={() => setShowPRForm(!showPRForm)}
            style={{ height: 30, fontSize: 12, width: "100%", background: "transparent", borderRadius: 6, cursor: "pointer", border: "0.5px solid #cfd8d1", marginBottom: showPRForm ? 8 : 0 }}
          >Open pull request</button>

          {showPRForm && (
            <div>
              <input
                value={prTitle}
                onChange={(e) => setPrTitle(e.target.value)}
                placeholder="PR title"
                style={{ width: "100%", height: 30, fontSize: 12, border: "0.5px solid #ccc", borderRadius: 6, padding: "0 8px", marginBottom: 6 }}
              />
              <button onClick={handlePR} disabled={!prTitle.trim()} style={{ height: 28, fontSize: 11.5, width: "100%", background: "#059669", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer" }}>
                Create PR
              </button>
            </div>
          )}

          {/* Recent commits */}
          {gitStatus?.last_commit_sha && (
            <div style={{ marginTop: 14 }}>
              <div style={{ fontSize: 10.5, color: "#73827a", textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 7 }}>Recent commits</div>
              <div style={{ fontSize: 12, color: "#5b6d62" }}>
                <span style={{ fontFamily: "monospace", color: "#73827a", fontSize: 10.5 }}>{gitStatus.last_commit_sha.slice(0, 7)}</span>{" "}
                {gitStatus.last_commit_message ?? "(no message)"}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
