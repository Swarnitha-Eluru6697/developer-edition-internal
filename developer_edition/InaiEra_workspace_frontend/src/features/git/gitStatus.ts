import type { GitStatus } from "../../types";

/** "owner/repo" from a repository URL (the last two path segments). */
export function repoShortName(repoUrl: string): string {
  return repoUrl.split("/").slice(-2).join("/");
}

/** "github.com/owner" — the host + first path segment, for the status bar. */
export function repoHostLabel(repoUrl: string | null | undefined): string | undefined {
  return repoUrl?.replace("https://", "").split("/").slice(0, 2).join("/");
}

/** Number of uncommitted files (modified + untracked) — the Changes tab badge. */
export function dirtyFileCount(status: GitStatus | undefined): number {
  if (!status) return 0;
  return status.modified_files.length + status.untracked_files.length;
}
