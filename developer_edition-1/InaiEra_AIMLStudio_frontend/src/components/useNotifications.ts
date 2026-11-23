/*
 * What the topbar bell lists, built from read-only endpoints that already exist —
 * there is no notifications table behind it:
 *
 *  - GET /alerts/active      FIRED alerts, and BUILDING ones (breaching, not yet
 *                            enough windows to fire) at lower priority  → /alerts
 *  - GET /promotions/pending versions waiting on a promotion decision   → /deploy
 *  - GET /training-runs      runs that failed, or finished, recently    → /training-runs/:id
 *
 * Only sources whose target page the user can reach are fetched at all, so a
 * user without mlstudio:alerts:view never sees (or asks for) alerts. "Unread" is purely local:
 * anything newer than the lastSeen stamp in localStorage, which only "Mark all as
 * read" moves — opening the panel doesn't.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";
import type { ActiveAlert, PendingPromotion, TrainingRun } from "../types/domain";

export type NotificationKind = "alert" | "alert-building" | "promotion" | "run-failed" | "run-succeeded";

export interface AppNotification {
  id: string;
  kind: NotificationKind;
  title: string;
  detail: string;
  /** ISO time; null when the source carries none (it then never counts as unread). */
  time: string | null;
  to: string;
}

const LS_LAST_SEEN = "mls_notifications_last_seen";
// Notifications the user has opened — removed from the list for good.
const LS_OPENED = "mls_notifications_opened";
const MAX_OPENED = 500;
const MAX_ITEMS = 20;
const REFRESH_MS = 30000;
// Runs (and the candidates they registered) older than this are history, not news.
const RECENT_MS = 7 * 24 * 3600 * 1000;

function readLastSeen(): number {
  try { return Number(localStorage.getItem(LS_LAST_SEEN)) || 0; } catch { return 0; }
}
function writeLastSeen(ms: number) {
  try { localStorage.setItem(LS_LAST_SEEN, String(ms)); } catch { /* storage disabled — read state just won't persist */ }
}

function readOpened(): Set<string> {
  try {
    const ids: unknown = JSON.parse(localStorage.getItem(LS_OPENED) || "[]");
    return new Set(Array.isArray(ids) ? ids.filter((x): x is string => typeof x === "string") : []);
  } catch { return new Set(); }
}
function writeOpened(ids: Set<string>) {
  try { localStorage.setItem(LS_OPENED, JSON.stringify([...ids].slice(-MAX_OPENED))); } catch { /* storage disabled — it just won't persist */ }
}

const ms = (iso: string | null) => (iso ? Date.parse(iso) || 0 : 0);

function fromAlert(a: ActiveAlert): AppNotification {
  const fired = a.status === "FIRED";
  return {
    // raisedAt resets on a new breach, so a re-fired alert is a new notification.
    id: `alert:${a.modelId}:${a.policyId}:${a.raisedAt}`,
    kind: fired ? "alert" : "alert-building",
    title: `${fired ? "Alert fired" : "Alert building"}: ${a.label}`,
    detail: `${a.modelId} · ${a.currentValue} (${a.condition})` +
      (fired ? (a.ackState === "acknowledged" ? " · acknowledged" : "") : ` · ${a.windowsExceeded}/${a.windowsToFire} windows`),
    time: a.raisedAt,
    to: "/alerts",
  };
}

// A promotion carries no timestamp, but its note names the run that registered
// it ("run_c15399e8 — trained from …"), and that run's createdAt is the closest
// real time there is. Without a matching run it's listed undated.
const runRef = (note: string) => /\brun_([0-9a-f]{8})/.exec(note || "")?.[1] ?? null;

function fromPromotion(p: PendingPromotion, run: TrainingRun | undefined): AppNotification {
  return {
    id: `promotion:${p.modelId}:${p.candidateVersion}`,
    kind: "promotion",
    title: `Awaiting promotion: ${p.modelId} v${p.candidateVersion}`,
    detail: p.currentChampionVersion != null ? `Challenger to champion v${p.currentChampionVersion}` : "No champion yet — first version to promote",
    time: run?.createdAt ?? null,
    to: "/deploy",
  };
}

function fromRun(r: TrainingRun): AppNotification {
  const failed = r.status === "failed";
  return {
    id: `run:${r.id}`,
    kind: failed ? "run-failed" : "run-succeeded",
    title: `${failed ? "Training failed" : "Training finished"}: ${r.modelName}`,
    detail: failed ? (r.error || "The run failed without a recorded reason") : `${r.baseModelId} on ${r.datasetName}`,
    time: r.createdAt,
    to: `/training-runs/${r.id}`,
  };
}

export function useNotifications() {
  const { can, canReach } = usePermissions();
  const [items, setItems] = useState<AppNotification[] | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [lastSeen, setLastSeen] = useState(readLastSeen);
  const [opened, setOpened] = useState(readOpened);
  // Drops a response that lands after a newer request (e.g. once permissions load).
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    const mine = ++seq.current;
    const wantAlerts = canReach("/alerts");
    const wantPromotions = canReach("/deploy");
    const wantRuns = can(PERM.pipelines) || can(PERM.train);
    setLoading(true);
    try {
      const [alerts, promotions, runs] = await Promise.all([
        wantAlerts ? api.getActiveAlerts() : Promise.resolve([] as ActiveAlert[]),
        wantPromotions ? api.getPendingPromotions() : Promise.resolve([] as PendingPromotion[]),
        wantRuns ? api.getTrainingRuns() : Promise.resolve([] as TrainingRun[]),
      ]);
      if (mine !== seq.current) return;
      const since = Date.now() - RECENT_MS;
      const runsByRef = new Map(runs.map((r) => [r.id.slice(0, 8), r]));
      const promoted = new Set<string>();
      const promotionItems = promotions.flatMap((p) => {
        const ref = runRef(p.note);
        const run = ref ? runsByRef.get(ref) : undefined;
        if (run) promoted.add(run.id);
        // Undated candidates are kept; dated ones only while recent.
        return run && ms(run.createdAt) < since ? [] : [fromPromotion(p, run)];
      });
      const runItems = runs
        // A finished run that left a candidate is already listed as that candidate.
        .filter((r) => (r.status === "failed" || (r.status === "succeeded" && !promoted.has(r.id))) && ms(r.createdAt) >= since)
        .map(fromRun);
      const all = [...alerts.map(fromAlert), ...promotionItems, ...runItems]
        // Defensive: never list something the user couldn't open.
        .filter((n) => canReach(n.to))
        // Newest first; at equal times a fired alert outranks a building one.
        .sort((a, b) => ms(b.time) - ms(a.time) || Number(b.kind === "alert") - Number(a.kind === "alert"));
      setItems(all);
      setError(false);
    } catch {
      if (mine === seq.current) setError(true);
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [can, canReach]);

  // On mount, every 30s, and again whenever the permissions (so the reachable set) change.
  useEffect(() => {
    setItems(null);
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const markAllRead = useCallback(() => {
    const now = Date.now();
    writeLastSeen(now);
    setLastSeen(now);
  }, []);

  const markOpened = useCallback((id: string) => {
    setOpened((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev).add(id);
      writeOpened(next);
      return next;
    });
  }, []);

  const isUnread = useCallback((n: AppNotification) => n.time != null && ms(n.time) > lastSeen, [lastSeen]);
  const visible = items?.filter((n) => !opened.has(n.id)) ?? null;
  const unread = (visible ?? []).filter(isUnread).length;

  return { items: visible?.slice(0, MAX_ITEMS) ?? null, unread, error, loading, refresh, markAllRead, markOpened, isUnread };
}
