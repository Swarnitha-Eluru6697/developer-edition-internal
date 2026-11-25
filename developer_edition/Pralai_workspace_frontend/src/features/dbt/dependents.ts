/**
 * Upstream and downstream jobs: "if an upstream job fails, this job fails too" and "if this job fails, its downstream jobs fail too"
 * (a job link, not a data link). The dbt Runner stores each job's direct downstream links ("Dependents"); an upstream link is the same
 * link seen from the other end. These helpers follow the links so the UI can show everything a failure reaches and refuse a loop before
 * saving.
 */
import type { DbtProject, UUID } from "../../types";

type Links = Pick<DbtProject, "id" | "dependent_project_ids">;

export const UPSTREAM_HELP =
  "Every run of this job, scheduled or Run now, checks these jobs first (Dagster does). Each must have succeeded recently: within its " +
  "own schedule interval plus 5 minutes (at least 10 minutes; 10 if it has no schedule). If one failed, is out of date or has never " +
  "run, this job fails at start without running.";
export const DOWNSTREAM_HELP =
  "If this job fails or goes out of date, these jobs fail at their next run, and every job below them, until this job has a recent " +
  "success again.";

function walk(next: (id: UUID) => UUID[], start: UUID): UUID[] {
  const seen = new Set<UUID>([start]);
  const order: UUID[] = [];
  const queue: UUID[] = [start];
  while (queue.length) {
    for (const id of next(queue.shift()!)) {
      if (!seen.has(id)) { seen.add(id); order.push(id); queue.push(id); }
    }
  }
  return order;
}

/** Every job below `id` (its downstream jobs, theirs, ...), nearest first. */
export function projectsBelow(projects: Links[], id: UUID): UUID[] {
  const byId = new Map(projects.map((p) => [p.id, p.dependent_project_ids ?? []]));
  return walk((x) => byId.get(x) ?? [], id);
}

/** Every job above `id` (whose failure reaches it), nearest first. */
export function projectsAbove(projects: Links[], id: UUID): UUID[] {
  const parents = new Map<UUID, UUID[]>();
  for (const p of projects) for (const d of p.dependent_project_ids ?? []) parents.set(d, [...(parents.get(d) ?? []), p.id]);
  return walk((x) => parents.get(x) ?? [], id);
}

/** Stand-in id for a job that is being registered and so is not in the list yet. */
export const NEW_JOB = "__new_job__" as UUID;

/**
 * The links as they WOULD be after saving: every link into and out of the job replaced by the chosen lists. Judging a candidate against
 * this (not against what is saved) is what lets a job move from "downstream" to "upstream" in one edit, and makes a job the user is
 * choosing right now count.
 */
export function linksAfterEdit(projects: Links[], selfId: UUID | null, upstream: UUID[], downstream: UUID[]): Links[] {
  const self = selfId ?? NEW_JOB;
  const others = projects.filter((p) => p.id !== self).map((p) => {
    const kept = (p.dependent_project_ids ?? []).filter((d) => d !== self);
    return { id: p.id, dependent_project_ids: upstream.includes(p.id) ? [...kept, self] : kept };
  });
  return [...others, { id: self, dependent_project_ids: downstream }];
}

/** Why a job cannot be chosen: it is already on the other side, or choosing it would close a loop. */
export type LinkBlock = "other-side" | "loop";

export interface LinkLimits {
  /** Jobs that cannot be chosen as upstream. */
  upstream: Map<UUID, LinkBlock>;
  /** Jobs that cannot be chosen as downstream. */
  downstream: Map<UUID, LinkBlock>;
  /** Jobs above the chosen upstream jobs that are not chosen themselves (they fail this job too, through the chosen ones). */
  alsoAbove: UUID[];
  /** Jobs below the chosen downstream jobs that are not chosen themselves (they fail too, through the chosen ones). */
  alsoBelow: UUID[];
}

export function linkLimits(projects: Links[], selfId: UUID | null, upstream: UUID[], downstream: UUID[]): LinkLimits {
  const self = selfId ?? NEW_JOB;
  const graph = linksAfterEdit(projects, selfId, upstream, downstream);
  const below = projectsBelow(graph, self);
  const above = projectsAbove(graph, self);
  const limits: LinkLimits = { upstream: new Map(), downstream: new Map(), alsoAbove: [], alsoBelow: [] };
  for (const p of projects) {
    if (p.id === self) continue;
    // chosen on one side: it may not also be chosen on the other (that is a loop of length two)
    if (downstream.includes(p.id)) limits.upstream.set(p.id, "other-side");
    else if (below.includes(p.id)) limits.upstream.set(p.id, "loop");        // already fails when this job fails
    if (upstream.includes(p.id)) limits.downstream.set(p.id, "other-side");
    else if (above.includes(p.id)) limits.downstream.set(p.id, "loop");      // this job already fails when it fails
  }
  limits.alsoAbove = above.filter((id) => !upstream.includes(id));
  limits.alsoBelow = below.filter((id) => !downstream.includes(id));
  return limits;
}
