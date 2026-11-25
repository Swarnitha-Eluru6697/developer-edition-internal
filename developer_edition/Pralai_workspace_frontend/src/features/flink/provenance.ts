/**
 * Git provenance of a FlinkSQL job (W-015).
 *
 * The runner records where a job's SQL was imported from as typed fields (`job.source`: canonical repository URL, the ref that was checked
 * out, the ref the user asked for, the SQL path, the immutable commit, the import time). That is what this reads. Jobs imported before
 * that existed carry the information in a trailing "[flink-git] url=… ref=… path=… sha=…" line of their description; it is still READ for
 * those jobs (nothing writes it any more, and the backend removes it the next time such a job is synced), so a legacy job keeps its
 * repository/ref display until then.
 */
import type { FlinkJob } from "../../types";

export interface Provenance {
  url?: string;
  /** The ref to show/sync: the one the user asked for, else the branch that was actually checked out. */
  ref?: string;
  /** The branch/tag that was actually checked out (differs from `requestedRef` when the default branch was used). */
  checkedOutRef?: string;
  requestedRef?: string;
  path?: string;
  sha?: string;
  importedAt?: string;
  provider?: string;
}

const GIT_LINE = /^\[flink-git\]([^\n]*)$/m;
const PROVENANCE_KEYS: ReadonlySet<string> = new Set(["url", "ref", "path", "sha"]);

/** Legacy: the marker line in a description. */
export function parseProvenance(description: string | null | undefined): Provenance {
  const match = description ? GIT_LINE.exec(description) : null;
  if (!match) return {};
  const out: Record<string, string> = {};
  for (const token of match[1].trim().split(" ")) {
    const i = token.indexOf("=");
    if (i > 0) out[token.slice(0, i)] = token.slice(i + 1);
  }
  return Object.fromEntries(Object.entries(out).filter(([k]) => PROVENANCE_KEYS.has(k))) as Provenance;
}

/** The job's provenance: the structured `source` when the runner has one, else the legacy marker, else {}. */
export function provenanceOf(job: Pick<FlinkJob, "source" | "description">): Provenance {
  const s = job.source;
  if (s?.git_url) {
    return {
      url: s.git_url,
      ref: s.requested_ref ?? s.git_ref ?? undefined,
      checkedOutRef: s.git_ref ?? undefined,
      requestedRef: s.requested_ref ?? undefined,
      path: s.sql_path ?? undefined,
      sha: s.commit_sha,
      importedAt: s.imported_at ?? undefined,
      provider: s.provider ?? undefined,
    };
  }
  return parseProvenance(job.description);
}

/** Everything the user actually wrote, minus a legacy machine-generated marker line. */
export function userDescription(description: string | null | undefined): string {
  return (description ?? "").replace(GIT_LINE, "").trim();
}
