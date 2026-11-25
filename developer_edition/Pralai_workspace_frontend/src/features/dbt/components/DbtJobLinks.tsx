/**
 * Choose a job's Upstream Jobs and Downstream Jobs. Used by the register drawer (dbt, SQL and Python) and by the job's Dependents tab.
 *
 *  - Upstream Jobs: if any of them fails, this job fails too.
 *  - Downstream Jobs: if this job fails, they fail too (and everything below them).
 *
 * The two lists are two tabs side by side (Upstream Jobs | Downstream Jobs), styled like the Files | Changes tabs of the file panel, with the
 * number of jobs chosen on each; both stay mounted, the other one is only hidden, so nothing chosen is lost by switching.
 *
 * A job already chosen on one side, or one that would close a loop, stays in the other list but greyed out with the reason, so the list
 * never shifts and nobody wonders where a job went. Jobs reached only through the chosen ones are listed underneath.
 * A job of ANOTHER kind than the one being edited carries a tag (dbt / SQL / Python); jobs of the same kind are plain.
 */
import { useState } from "react";
import type { DbtProject, UUID } from "../../../types";
import { hiddenScrollbar } from "../../../shared/ui/drawerStyles";
import { DOWNSTREAM_HELP, UPSTREAM_HELP, linkLimits, type LinkBlock } from "../dependents";
import { kindOf, type JobKind } from "../jobKinds";

export interface JobLinks { upstream: UUID[]; downstream: UUID[] }

interface Props {
  /** null while registering (the job does not exist yet). */
  selfId: UUID | null;
  /** The kind of job being registered or edited: same-kind jobs in the lists are plain, other kinds get a tag. */
  kind: JobKind;
  projects: DbtProject[];
  value: JobLinks;
  onChange: (next: JobLinks) => void;
}

const TAGS: Record<JobKind, { label: string; color: string; background: string }> = {
  dbt: { label: "dbt", color: "var(--ws-ok, #2F6B14)", background: "var(--ws-ok-bg, #eaf3e1)" },
  sql: { label: "SQL", color: "var(--ws-info, #1e40af)", background: "var(--ws-info-bg, #e0e7ff)" },
  python: { label: "Python", color: "var(--ws-purple, #6b21a8)", background: "var(--ws-purple-bg, #f3e8ff)" },
};

const WHY: Record<"upstream" | "downstream", Record<LinkBlock, string>> = {
  upstream: { "other-side": "already downstream", loop: "would make a loop" },
  downstream: { "other-side": "already upstream", loop: "would make a loop" },
};

interface ListProps {
  side: "upstream" | "downstream";
  title: string;
  hint: string;
  projects: DbtProject[];
  kind: JobKind;
  value: UUID[];
  blocked: Map<UUID, LinkBlock>;
  also: UUID[];
  alsoText: string;
  onToggle: (id: UUID, on: boolean) => void;
}

function JobList({ side, title, hint, projects, kind, value, blocked, also, alsoText, onToggle }: ListProps) {
  const names = new Map(projects.map((p) => [p.id, p.name]));
  return (
    <div>
      <div role="group" aria-label={title}
        style={{ border: "0.5px solid var(--ws-line-strong, #cfd8d1)", borderRadius: 6, maxHeight: 200, overflowY: "auto", background: "var(--ws-surface, #fff)", ...hiddenScrollbar }}>
        {projects.map((p) => {
          const why = blocked.get(p.id);
          const tag = kindOf(p) !== kind ? TAGS[kindOf(p)] : null;
          return (
            <label key={p.id} title={why ? `${p.name}: ${WHY[side][why]}` : undefined}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", fontSize: 12.5, borderBottom: "0.5px solid var(--ws-line, #eef2ed)",
                cursor: why ? "not-allowed" : "pointer", color: why ? "var(--ws-subtle, #73827a)" : "var(--ws-text, #14201B)" }}>
              <input type="checkbox" checked={value.includes(p.id)} disabled={!!why}
                onChange={(e) => onToggle(p.id, e.target.checked)} />
              <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.name}</span>
              {tag && <span data-testid="kind-tag" style={{ fontSize: 10.5, fontWeight: 600, color: tag.color, background: tag.background, borderRadius: 4, padding: "0 5px" }}>{tag.label}</span>}
              {why && <span style={{ fontSize: 11 }}>{WHY[side][why]}</span>}
            </label>
          );
        })}
      </div>
      {also.length > 0 && (
        <div data-testid={`${side}-also`} style={{ fontSize: 11.5, color: "var(--ws-muted, #5b6d62)", marginTop: 6 }}>
          {alsoText}: {also.map((id) => names.get(id) ?? id).join(", ")}
        </div>
      )}
      <div style={{ fontSize: 11, color: "var(--ws-subtle, #73827a)", marginTop: 3, lineHeight: 1.45 }}>{hint}</div>
    </div>
  );
}

type LinkTab = "upstream" | "downstream";

/** Upstream Jobs | Downstream Jobs, the way Files | Changes sit in the file panel: equal halves, an orange line under the open one, and
 *  a small count of the jobs chosen on each side. */
function LinkTabs({ tab, onTab, counts }: { tab: LinkTab; onTab: (t: LinkTab) => void; counts: Record<LinkTab, number> }) {
  const TITLES: Record<LinkTab, string> = { upstream: "Upstream Jobs", downstream: "Downstream Jobs" };
  return (
    <div role="tablist" style={{ display: "flex", borderBottom: "0.5px solid var(--ws-line, #E3E8E2)", marginBottom: 12 }}>
      {(["upstream", "downstream"] as const).map((t) => (
        <button key={t} type="button" role="tab" id={`job-links-tab-${t}`} aria-selected={tab === t} aria-controls={`job-links-panel-${t}`}
          onClick={() => onTab(t)}
          style={{
            flex: 1, textAlign: "center", padding: "7px 0", fontSize: 12.5, cursor: "pointer", background: "none", border: "none",
            fontFamily: "inherit", color: tab === t ? "var(--ws-text, #14201B)" : "var(--ws-subtle, #888)", fontWeight: tab === t ? 500 : 400,
            borderBottom: tab === t ? "2px solid #0A7D5A" : "2px solid transparent",
            display: "flex", alignItems: "center", justifyContent: "center", gap: 5,
          }}>
          {TITLES[t]}
          {counts[t] > 0 && (
            <span data-testid={`${t}-count`} style={{ background: "var(--ws-warn-bg, #fdebdd)", color: "var(--ws-warn, #a8470a)", padding: "1px 5px", borderRadius: 4, fontSize: 10.5, fontWeight: 600 }}>
              {counts[t]}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

export function DbtJobLinks({ selfId, kind, projects, value, onChange }: Props) {
  const [tab, setTab] = useState<LinkTab>("upstream");
  const others = projects.filter((p) => p.id !== selfId).sort((a, b) => a.name.localeCompare(b.name));
  if (!others.length) {
    return <div style={{ fontSize: 12, color: "var(--ws-subtle, #73827a)" }}>No other jobs registered yet.</div>;
  }
  const limits = linkLimits(projects, selfId, value.upstream, value.downstream);
  const toggle = (side: keyof JobLinks) => (id: UUID, on: boolean) =>
    onChange({ ...value, [side]: on ? [...value[side], id] : value[side].filter((v) => v !== id) });
  const panel = (t: LinkTab) => ({ role: "tabpanel", id: `job-links-panel-${t}`, "aria-labelledby": `job-links-tab-${t}`, style: { display: tab === t ? undefined : "none" } });
  return (
    <>
      <LinkTabs tab={tab} onTab={setTab} counts={{ upstream: value.upstream.length, downstream: value.downstream.length }} />
      <div {...panel("upstream")}>
        <JobList side="upstream" title="Upstream Jobs" hint={UPSTREAM_HELP} projects={others} kind={kind} value={value.upstream}
          blocked={limits.upstream} also={limits.alsoAbove} alsoText="Also fails this job, through their own upstream jobs" onToggle={toggle("upstream")} />
      </div>
      <div {...panel("downstream")}>
        <JobList side="downstream" title="Downstream Jobs" hint={DOWNSTREAM_HELP} projects={others} kind={kind} value={value.downstream}
          blocked={limits.downstream} also={limits.alsoBelow} alsoText="Also fails, through their own downstream jobs" onToggle={toggle("downstream")} />
      </div>
    </>
  );
}
