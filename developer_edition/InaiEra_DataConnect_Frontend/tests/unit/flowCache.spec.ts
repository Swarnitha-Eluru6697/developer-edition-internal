/*
 * FE-02: the saved runtime snapshot is a stand-in, never a live reading — timestamped, scoped to one backend + organisation, expiring, and
 * ignored when it is unreadable, foreign or from the future.
 */
import { test, expect } from "@playwright/test";
import {
  FLOW_CACHE_MAX_AGE_MS, flowCacheKey, flowFreshness, pruneFlowCache, readFlowCache, writeFlowCache, type FlowCacheStorage,
} from "../../src/flowCache";
import type { RuntimeFlow } from "../../src/types/api";

class MemoryStorage implements FlowCacheStorage {
  data = new Map<string, string>();
  get length() { return this.data.size; }
  key(i: number) { return [...this.data.keys()][i] ?? null; }
  getItem(k: string) { return this.data.get(k) ?? null; }
  setItem(k: string, v: string) { this.data.set(k, v); }
  removeItem(k: string) { this.data.delete(k); }
}

const NOW = Date.parse("2026-10-13T12:00:00Z");
const A = { baseUrl: "https://a.example/api", orgId: "org_a" };
const FLOW: RuntimeFlow = {
  reachable: true, connect_url: "u", bootstrap: "b",
  connectors: [{ name: "c1", pipeline_id: "p1", type: "source", state: "RUNNING" }], topics: [], sinks: [],
};
const minutesAgo = (m: number) => NOW - m * 60_000;

test.describe("the key", () => {
  test("is different for every backend and organisation, and cannot be forged by a separator inside either", () => {
    const keys = new Set([
      flowCacheKey(A), flowCacheKey({ ...A, orgId: "org_b" }), flowCacheKey({ ...A, baseUrl: "https://b.example/api" }),
      flowCacheKey({ baseUrl: "x:y", orgId: "z" }), flowCacheKey({ baseUrl: "x", orgId: "y:z" }),
    ]);
    expect(keys.size).toBe(5);
  });
});

test.describe("write then read", () => {
  test("a snapshot comes back with the time it was observed", () => {
    const s = new MemoryStorage();
    writeFlowCache(s, A, FLOW, minutesAgo(5), NOW);
    const hit = readFlowCache(s, A, NOW);
    expect(hit?.flow).toEqual(FLOW);
    expect(hit?.observedAt).toBe(new Date(minutesAgo(5)).toISOString());
  });

  test("another organisation or another backend never sees it (isolation)", () => {
    const s = new MemoryStorage();
    writeFlowCache(s, A, FLOW, minutesAgo(1), NOW);
    expect(readFlowCache(s, { ...A, orgId: "org_b" }, NOW)).toBeUndefined();
    expect(readFlowCache(s, { ...A, baseUrl: "https://prod.example/api" }, NOW)).toBeUndefined();
  });

  test("an entry copied under the wrong key is still refused, because the scope is stored inside it too", () => {
    const s = new MemoryStorage();
    writeFlowCache(s, A, FLOW, minutesAgo(1), NOW);
    const B = { ...A, orgId: "org_b" };
    s.setItem(flowCacheKey(B), s.getItem(flowCacheKey(A))!);
    expect(readFlowCache(s, B, NOW)).toBeUndefined();
  });

  test("a snapshot older than the maximum age is ignored and removed", () => {
    const s = new MemoryStorage();
    writeFlowCache(s, A, FLOW, NOW - FLOW_CACHE_MAX_AGE_MS - 1000, NOW);
    expect(readFlowCache(s, A, NOW)).toBeUndefined();
    expect(s.getItem(flowCacheKey(A))).toBeNull();
  });

  test("one just inside the maximum age is still shown", () => {
    const s = new MemoryStorage();
    writeFlowCache(s, A, FLOW, NOW - FLOW_CACHE_MAX_AGE_MS + 1000, NOW);
    expect(readFlowCache(s, A, NOW)).toBeDefined();
  });

  test("a snapshot dated in the future is ignored (a wrong clock must not make old data look fresh)", () => {
    const s = new MemoryStorage();
    writeFlowCache(s, A, FLOW, NOW + 10 * 60_000, NOW);
    expect(readFlowCache(s, A, NOW)).toBeUndefined();
  });

  test("a little clock skew is tolerated", () => {
    const s = new MemoryStorage();
    writeFlowCache(s, A, FLOW, NOW + 20_000, NOW);
    expect(readFlowCache(s, A, NOW)).toBeDefined();
  });
});

test.describe("what is never trusted", () => {
  const entry = (over: Record<string, unknown>) => JSON.stringify({ v: 2, observed_at: new Date(minutesAgo(1)).toISOString(), base_url: A.baseUrl, org_id: A.orgId, flow: FLOW, ...over });

  test("the old unscoped, untimestamped snapshot is deleted and never read", () => {
    const s = new MemoryStorage();
    s.setItem("dc.pipelines.lastFlow", JSON.stringify(FLOW));
    expect(readFlowCache(s, A, NOW)).toBeUndefined();
    expect(s.getItem("dc.pipelines.lastFlow")).toBeNull();
  });

  for (const [name, raw] of Object.entries({
    "not JSON": "{oops",
    "no timestamp": entry({ observed_at: undefined }),
    "an unparseable timestamp": entry({ observed_at: "yesterday-ish" }),
    "a timestamp of the wrong type": entry({ observed_at: 12345 }),
    "another version": entry({ v: 1 }),
    "a flow of the wrong shape": entry({ flow: { reachable: "yes" } }),
    "no flow": entry({ flow: undefined }),
    "an array": "[]",
  })) {
    test(`${name} gives nothing and is cleaned up`, () => {
      const s = new MemoryStorage();
      s.setItem(flowCacheKey(A), raw);
      expect(readFlowCache(s, A, NOW)).toBeUndefined();
      expect(s.getItem(flowCacheKey(A))).toBeNull();
    });
  }
});

test.describe("housekeeping", () => {
  test("writing clears expired entries left by other organisations, and keeps current ones and unrelated keys", () => {
    const s = new MemoryStorage();
    const old = { ...A, orgId: "old_org" }, recent = { ...A, orgId: "recent_org" };
    writeFlowCache(s, old, FLOW, NOW - FLOW_CACHE_MAX_AGE_MS - 5000, NOW - 10 * 3600_000);
    writeFlowCache(s, recent, FLOW, NOW - 1000, NOW);
    s.setItem("dc.pipelines.lastFlow:2:garbage", "not json");
    s.setItem("unrelated", "keep");
    writeFlowCache(s, A, FLOW, NOW, NOW);
    expect(s.getItem(flowCacheKey(old))).toBeNull();
    expect(s.getItem("dc.pipelines.lastFlow:2:garbage")).toBeNull();
    expect(s.getItem(flowCacheKey(recent))).not.toBeNull();
    expect(s.getItem(flowCacheKey(A))).not.toBeNull();
    expect(s.getItem("unrelated")).toBe("keep");
  });

  test("pruning alone also works", () => {
    const s = new MemoryStorage();
    writeFlowCache(s, A, FLOW, NOW - FLOW_CACHE_MAX_AGE_MS - 1, NOW - 10 * 3600_000);
    pruneFlowCache(s, NOW);
    expect(s.length).toBe(0);
  });
});

test.describe("blocked or broken storage never breaks the page", () => {
  const broken: FlowCacheStorage = {
    get length(): number { throw new Error("blocked"); },
    key() { throw new Error("blocked"); }, getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("quota"); }, removeItem() { throw new Error("blocked"); },
  };
  test("reads give nothing and writes are silent", () => {
    expect(() => writeFlowCache(broken, A, FLOW, NOW)).not.toThrow();
    expect(readFlowCache(broken, A, NOW)).toBeUndefined();
  });
});

test.describe("flowFreshness — what the page says about its numbers", () => {
  const cached = { flow: FLOW, observedAt: new Date(minutesAgo(7)).toISOString() };

  test("a live answer is live", () => {
    expect(flowFreshness({ live: FLOW, liveObservedAt: NOW, failed: false, cached })).toEqual({ kind: "live" });
  });
  test("nothing yet and nothing saved: nothing to flag", () => {
    expect(flowFreshness({ live: undefined, liveObservedAt: undefined, failed: false, cached: undefined })).toEqual({ kind: "live" });
  });
  test("a saved snapshot shown while the live answer loads is 'restored', with its observed time", () => {
    expect(flowFreshness({ live: undefined, liveObservedAt: undefined, failed: false, cached })).toEqual({ kind: "restored", observedAt: cached.observedAt });
  });
  test("a failed refresh with an earlier live answer is stale, dated by that answer", () => {
    expect(flowFreshness({ live: FLOW, liveObservedAt: minutesAgo(2), failed: true, cached })).toEqual({ kind: "stale", observedAt: new Date(minutesAgo(2)).toISOString() });
  });
  test("a failed refresh after a reload is stale, dated by the saved snapshot", () => {
    expect(flowFreshness({ live: undefined, liveObservedAt: undefined, failed: true, cached })).toEqual({ kind: "stale", observedAt: cached.observedAt });
  });
  test("a failed refresh with nothing earlier says the status is unavailable, not stale", () => {
    expect(flowFreshness({ live: undefined, liveObservedAt: undefined, failed: true, cached: undefined })).toEqual({ kind: "unavailable" });
  });
  test("an answer saying Kafka Connect is unreachable is flagged with the backend's message", () => {
    expect(flowFreshness({ live: { ...FLOW, reachable: false, message: "refused" }, liveObservedAt: NOW, failed: false, cached }))
      .toEqual({ kind: "unreachable", message: "refused" });
    expect(flowFreshness({ live: { ...FLOW, reachable: false }, liveObservedAt: NOW, failed: false, cached: undefined }))
      .toMatchObject({ kind: "unreachable", message: "Kafka Connect could not be reached." });
  });
});
