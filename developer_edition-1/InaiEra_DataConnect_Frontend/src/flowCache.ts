/*
 * FE-02: the last good runtime-flow snapshot the Pipelines page keeps in browser storage, so a reload shows the previous counts instead of flashing
 * zeros while /runtime/flow answers. A snapshot is only ever a stand-in, so it is never allowed to pass for current status:
 *   - it records WHEN it was observed (`observed_at`), so the page can say how old it is;
 *   - it is stored per backend + organisation, so switching org or deployment never shows another tenant's or environment's data;
 *   - it expires (FLOW_CACHE_MAX_AGE_MS), and an entry that is unreadable, from another scope or dated in the future is ignored, not trusted.
 * Storage is injected so this is testable without a browser; every storage call is guarded because storage can be blocked or full.
 */
import { isRecord, parseJson, stringOf } from "./json";
import { runtimeFlowOf } from "./pipelineAdapters";
import type { RuntimeFlow } from "./types/api";

/** Older than this, a stored snapshot is more misleading than showing nothing. */
export const FLOW_CACHE_MAX_AGE_MS = 60 * 60 * 1000;
/** Tolerated clock difference before an `observed_at` is considered to be in the future. */
const CLOCK_SKEW_MS = 60 * 1000;

const KEY_PREFIX = "dc.pipelines.lastFlow";
/** The pre-FE-02 key: a bare snapshot with no timestamp and no scope. Impossible to age or attribute, so it is removed, never read. */
const LEGACY_KEY = KEY_PREFIX;
const VERSION = 2;

export interface FlowCacheScope { baseUrl: string; orgId: string }
export type FlowCacheStorage = Pick<Storage, "getItem" | "setItem" | "removeItem" | "key" | "length">;
export interface CachedFlow { flow: RuntimeFlow; observedAt: string }

export function flowCacheKey(scope: FlowCacheScope): string {
  return `${KEY_PREFIX}:${VERSION}:${encodeURIComponent(scope.baseUrl)}:${encodeURIComponent(scope.orgId)}`;
}

function attempt<T>(fn: () => T): T | undefined {
  try { return fn(); } catch { return undefined; }
}

function ageMs(observedAt: string, now: number): number | null {
  const t = Date.parse(observedAt);
  return Number.isNaN(t) ? null : now - t;
}

function parseEntry(raw: string | null, scope: FlowCacheScope, now: number): CachedFlow | undefined {
  if (!raw) return undefined;
  const parsed = parseJson(raw);
  if (!parsed.ok || !isRecord(parsed.value)) return undefined;
  const entry = parsed.value;
  const observedAt = stringOf(entry.observed_at);
  if (entry.v !== VERSION || observedAt === undefined) return undefined;
  if (entry.base_url !== scope.baseUrl || entry.org_id !== scope.orgId) return undefined;       // belt and braces: the key already scopes it
  const age = ageMs(observedAt, now);
  if (age === null || age < -CLOCK_SKEW_MS || age > FLOW_CACHE_MAX_AGE_MS) return undefined;
  const flow = runtimeFlowOf(entry.flow);
  return flow ? { flow, observedAt } : undefined;
}

/** The snapshot for exactly this backend + organisation, or undefined when there is none worth showing. */
export function readFlowCache(storage: FlowCacheStorage, scope: FlowCacheScope, now: number = Date.now()): CachedFlow | undefined {
  attempt(() => storage.removeItem(LEGACY_KEY));
  const key = flowCacheKey(scope);
  const raw = attempt(() => storage.getItem(key)) ?? null;
  const hit = parseEntry(raw, scope, now);
  if (!hit && raw) attempt(() => storage.removeItem(key));                                       // expired or unreadable: drop it
  return hit;
}

/** Remember a snapshot observed at `observedAt` (epoch ms), and clear out expired entries left by other scopes. */
export function writeFlowCache(storage: FlowCacheStorage, scope: FlowCacheScope, flow: RuntimeFlow, observedAt: number, now: number = Date.now()): void {
  attempt(() => storage.setItem(flowCacheKey(scope), JSON.stringify({
    v: VERSION, observed_at: new Date(observedAt).toISOString(), base_url: scope.baseUrl, org_id: scope.orgId, flow,
  })));
  pruneFlowCache(storage, now);
}

/** Remove every stored snapshot that has expired, whichever scope wrote it. */
export function pruneFlowCache(storage: FlowCacheStorage, now: number): void {
  attempt(() => {
    const doomed: string[] = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      if (key === null || !key.startsWith(`${KEY_PREFIX}:`)) continue;
      const parsed = parseJson(storage.getItem(key) ?? "");
      const observedAt = parsed.ok && isRecord(parsed.value) ? stringOf(parsed.value.observed_at) : undefined;
      const age = observedAt === undefined ? null : ageMs(observedAt, now);
      if (age === null || age > FLOW_CACHE_MAX_AGE_MS) doomed.push(key);
    }
    doomed.forEach((key) => storage.removeItem(key));
  });
}

/** What the Pipelines page should tell the user about the runtime numbers it is showing. */
export type FlowFreshness =
  | { kind: "live" }
  | { kind: "restored"; observedAt: string }                       // a stored snapshot shown while the live answer is still on its way
  | { kind: "stale"; observedAt: string }                          // the latest refresh failed; these are the last values that were seen
  | { kind: "unavailable" }                                        // the latest refresh failed and there is nothing earlier to show
  | { kind: "unreachable"; message: string };                      // the backend answered, but could not reach Kafka Connect

export function flowFreshness(input: {
  live: RuntimeFlow | undefined; liveObservedAt: number | undefined; failed: boolean; cached: CachedFlow | undefined;
}): FlowFreshness {
  const { live, liveObservedAt, failed, cached } = input;
  if (failed) {
    const observedAt = live && liveObservedAt !== undefined ? new Date(liveObservedAt).toISOString() : cached?.observedAt;
    return observedAt ? { kind: "stale", observedAt } : { kind: "unavailable" };
  }
  if (live) return live.reachable === false ? { kind: "unreachable", message: live.message || "Kafka Connect could not be reached." } : { kind: "live" };
  return cached ? { kind: "restored", observedAt: cached.observedAt } : { kind: "live" };
}
