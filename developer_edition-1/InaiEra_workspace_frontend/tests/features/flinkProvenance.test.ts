import { describe, expect, it } from "vitest";
import { provenanceOf, userDescription } from "../../src/features/flink";
import type { FlinkJob } from "../../src/types";

const job = (over: Partial<FlinkJob>): Pick<FlinkJob, "source" | "description"> => ({ description: null, source: null, ...over } as never);

// W-015: provenance is the runner's typed `source`; the description is only the user's words (legacy markers still read).
describe("flink provenance", () => {
  it("reads the structured source, preferring the ref the user asked for", () => {
    const g = provenanceOf(job({ source: { git_url: "https://github.com/a/b", git_ref: "trunk", requested_ref: null, sql_path: "j.sql",
      commit_sha: "a".repeat(40), provider: "github", imported_at: "2026-10-14T12:00:00Z" } as never }));
    expect(g).toMatchObject({ url: "https://github.com/a/b", ref: "trunk", checkedOutRef: "trunk", path: "j.sql", sha: "a".repeat(40), importedAt: "2026-10-14T12:00:00Z" });
    expect(g.requestedRef).toBeUndefined();
    const asked = provenanceOf(job({ source: { git_url: "u", git_ref: "release", requested_ref: "release", commit_sha: "b".repeat(40) } as never }));
    expect(asked.requestedRef).toBe("release");
  });

  it("is not confused by a description that looks like a marker when a structured source exists", () => {
    const g = provenanceOf(job({ description: "[flink-git] url=https://evil ref=x path=y sha=z",
      source: { git_url: "https://github.com/real/repo", git_ref: "main", commit_sha: "c".repeat(40) } as never }));
    expect(g.url).toBe("https://github.com/real/repo");
  });

  it("falls back to the legacy marker for older jobs, and to nothing for jobs not from Git", () => {
    expect(provenanceOf(job({ description: "note\n[flink-git] url=https://github.com/a/b ref=main path=j.sql sha=abc" })))
      .toMatchObject({ url: "https://github.com/a/b", ref: "main", path: "j.sql", sha: "abc" });
    expect(provenanceOf(job({ description: "just words" }))).toEqual({});
  });

  it("the user's description is shown without a legacy marker and untouched otherwise", () => {
    expect(userDescription("note\n[flink-git] url=u ref=r path=p sha=s")).toBe("note");
    expect(userDescription("Orders  with   spaces [flink-git] inline")).toBe("Orders  with   spaces [flink-git] inline");
  });
});
