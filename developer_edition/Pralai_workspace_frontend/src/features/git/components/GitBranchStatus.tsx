/** Branch chip + sync summary + repo label for the editor's bottom status bar. */
import { useGetGitBindingQuery, useGetGitStatusQuery } from "../api";
import { repoHostLabel } from "../gitStatus";
import type { UUID } from "../../../types";

export function GitBranchStatus({ workspaceId }: { workspaceId: UUID | null }) {
  const { data: gitStatus } = useGetGitStatusQuery(workspaceId ?? "", { skip: !workspaceId });
  const { data: gitBinding } = useGetGitBindingQuery(workspaceId ?? "", { skip: !workspaceId });
  return (
    <>
      <div
        style={{ display: "inline-flex", alignItems: "center", gap: 4, background: "var(--ws-surface, #fff)", border: "0.5px solid var(--ws-line-strong, #cfd8d1)", borderRadius: 4, padding: "2px 7px", fontFamily: "monospace", fontSize: 10.5, cursor: "pointer" }}
      >
        <i className="ti ti-git-branch" style={{ fontSize: 11 }} />{gitStatus?.branch ?? gitBinding?.current_branch ?? "main"}
      </div>
      {gitStatus && <span style={{ color: "var(--ws-ok, #2f6b14)" }}><i className="ti ti-check" style={{ fontSize: 11 }} /> Up to date</span>}
      {gitStatus && (gitStatus.modified_files.length > 0) && (
        <span style={{ color: "var(--ws-warn, #a8470a)" }}><i className="ti ti-circle-dot" style={{ fontSize: 11 }} /> {gitStatus.modified_files.length} uncommitted</span>
      )}
      <span style={{ marginLeft: "auto", color: "var(--ws-subtle, #73827a)" }}>
        {repoHostLabel(gitBinding?.repo_url)}
      </span>
    </>
  );
}
