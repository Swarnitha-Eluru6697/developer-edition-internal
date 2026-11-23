/*
 * Presentational primitives shared across every page — the React equivalents
 * of the vanilla app's HTML builder helpers (ph, statCards, pill, pipeRing …).
 */
import { createdAtTitle, formatCreatedAt } from "../dates";
import React, { useLayoutEffect, useRef, useState } from "react";
import { redactSecrets, redactJsonString } from "../secretRedaction";
import { errorMessage } from "../errors";

export interface StatCard { k?: string; l: React.ReactNode; v: React.ReactNode; s?: React.ReactNode; }

export function Pill({ children, cls = "bgr" }: { children: React.ReactNode; cls?: string }) {
  return <span className={`badge ${cls}`}>{children}</span>;
}

/**
 * Text that is cut at the edge of its cell with "…". Only when it really is cut does it carry its full text as a tooltip — a name that fits
 * shows nothing on hover. It re-measures when the text changes or the column is resized.
 */
export function CutText({ text, style }: { text: string; style?: React.CSSProperties }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [cut, setCut] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setCut(el.scrollWidth > el.clientWidth + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text]);
  return <span ref={ref} className="cell-cut" style={style} title={cut ? text : undefined}>{text}</span>;
}

/** The pipelines a schema contract or a transform policy is used by: the first two names, "+N" for the rest (all of them in the tooltip), "—" when none. */
export function PipelineNames({ names }: { names: readonly string[] }) {
  if (!names.length) return <span style={{ color: "var(--color-text-muted)" }} aria-label="No pipeline">—</span>;
  const shown = names.slice(0, 2).join(", "), more = names.length - 2;
  return <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={names.join(", ")}>{shown}{more > 0 ? <span style={{ color: "var(--color-text-secondary)" }}> +{more}</span> : null}</span>;
}

/** A "Created at" value: readable local time, full time on hover, a dash when the backend didn't say. */
export function CreatedAt({ value }: { value: string | null | undefined }) {
  return <span title={createdAtTitle(value)} style={{ whiteSpace: "nowrap", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{formatCreatedAt(value)}</span>;
}

export function PH({ title, sub, right = null }: { title: React.ReactNode; sub?: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="ph">
      <div>
        <div className="ptitle">{title}</div>
        {sub ? <div className="psub">{sub}</div> : null}
      </div>
      <div className="ph-r">{right}</div>
    </div>
  );
}

// cards: [{ k, l, v, s }]  (k = colour class bl/gn/rd/am/bp)
export function StatCards({ cards }: { cards: StatCard[] }) {
  return (
    <div className="stats" style={cards.length < 4 ? { gridTemplateColumns: `repeat(${cards.length}, 1fr)` } : undefined}>
      {cards.map((c, i) => (
        <div className={`sc ${c.k || "bl"}`} key={i}>
          <div className="sc-l">{c.l}</div>
          <div className="sc-v">{c.v}</div>
          {c.s ? <div className="sc-s">{c.s}</div> : null}
        </div>
      ))}
    </div>
  );
}

export function Loading() {
  return <div className="empty"><span className="spin" /> loading…</div>;
}

export function ErrBox({ error }: { error: unknown }) {
  const msg = errorMessage(error);
  return (
    <div className="alert alert-w">
      <div className="at" style={{ color: "#991b1b" }}>Failed to load</div>
      <div className="ab">{msg}</div>
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

// Pipeline progress donut — ported from pipeRing().
export function Ring({ state, pct }: { state?: string; pct?: number }) {
  const col = state === "running" ? "#22c55e" : state === "deployed" ? "#f97316" : state === "paused" ? "#f59e0b" : state === "restarting" ? "#f59e0b" : state === "failed" ? "#ef4444" : "#9ca3af";
  const C = Math.round(2 * Math.PI * 13);
  const p = Math.max(0, Math.min(100, Math.round(pct ?? 0)));
  const dash = Math.round((C * p) / 100);
  return (
    <div style={{ width: 34, height: 34, position: "relative", display: "grid", placeItems: "center", flexShrink: 0 }} title={`${p}% processed`}>
      <svg width="34" height="34" viewBox="0 0 34 34" style={{ transform: "rotate(-90deg)" }}>
        <circle cx="17" cy="17" r="13" fill="none" stroke="#e5e9ee" strokeWidth="3" />
        <circle cx="17" cy="17" r="13" fill="none" stroke={col} strokeWidth="3" strokeLinecap="round" strokeDasharray={`${dash} ${C}`} />
      </svg>
      <span style={{ position: "absolute", fontSize: 8, fontWeight: 600, color: col }}>{p}%</span>
    </div>
  );
}

export function ProgressBar({ pct, color = "#22c55e" }: { pct?: number; color?: string }) {
  const p = Math.max(0, Math.min(100, Math.round(pct ?? 0)));
  return (
    <div className="prog-bar" style={{ marginTop: 4 }}>
      <div className="pb-fill" style={{ width: `${p}%`, background: color }} />
    </div>
  );
}

export function CodeBlock({ value }: { value: unknown }) {
  // FE-BUG-09: CodeBlock is this app's one shared "dump an arbitrary
  // backend payload as JSON" renderer (a transform preview's generated
  // config, a schema-infer/drift result, a quality rule dump, ...) — a
  // frontend-side redaction pass here, independent of and in addition to
  // whatever the backend itself already redacted, is what makes every one
  // of those call sites safe by default rather than each needing its own
  // opt-in. See ../secretRedaction.ts.
  const safe = typeof value === "string" ? redactJsonString(value) : redactSecrets(value);
  const text = typeof safe === "string" ? safe : JSON.stringify(safe, null, 2);
  return <div className="codeblk">{text}</div>;
}

// Drawer content scaffold used inside the shared Modal shell.
export function Drawer({ title, onClose, children, footer }: {
  title: React.ReactNode;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <>
      <div className="mh">
        {/* FE-BUG-21: fixed id — Modal.tsx's shared shell points
            aria-labelledby at it. Safe as a fixed id because the app only
            ever has one modal open at a time (see AppContext.tsx's
            ModalProvider — a single node slot, not a stack). */}
        <div className="mt" id="modal-title">{title}</div>
        <button className="mx" onClick={onClose} aria-label="Close">×</button>
      </div>
      <div className="mb">{children}</div>
      {footer ? <div className="ff">{footer}</div> : null}
    </>
  );
}

// Small labelled-field wrapper for forms.
export function Field({ label, req, children, style }: {
  label: React.ReactNode;
  req?: boolean;
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div className="fld" style={style}>
      <label>{label}{req ? " *" : ""}</label>
      {children}
    </div>
  );
}

// FE-BUG-21: pairs with aria-invalid/aria-describedby on the actual
// input/select — role="alert" so a screen reader announces it the moment
// it appears, not only if the user happens to tab onto the field
// afterward. `id` must match what the input's aria-describedby points at.
export function FieldError({ id, message }: { id: string; message: string }) {
  return <div id={id} role="alert" className="field-error">{message}</div>;
}

// ── Icons ─────────────────────────────────────────────────────────────────────
// The platform's shared drawings — the same paths as Workspace's Glyph.tsx (and the Catalog): 24×24 grid, 2px round stroke, drawn at
// 15px, in `currentColor`. An icon takes its colour from the button around it (gray, and red on hover for delete — see `.ai` in
// styles.css), so every action looks the same on every page and in Workspace.
function Svg({ children }: { children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true" style={{ display: "block", flexShrink: 0 }}>{children}</svg>
  );
}
/** The same weight for the few drawings that are kept on a 16×16 grid: 1.33 on 16 is the 2 of the 24 grid. */
function Svg16({ children }: { children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true" style={{ display: "block", flexShrink: 0 }}>{children}</svg>
  );
}
export const IconView = () => (
  <Svg><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></Svg>
);
export const IconEdit = () => (
  <Svg><path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" /></Svg>
);
export const IconDel = () => (
  <Svg><path d="M4 7h16" /><path d="M10 4h4a1 1 0 0 1 1 1v2H9V5a1 1 0 0 1 1-1Z" /><path d="M6 7v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7" /><path d="M10 11v6M14 11v6" /></Svg>
);
/** Test (a connection): the plug, Workspace's `pi-test`. */
export const IconTest = () => (
  <Svg>
    <path d="M7 12l5 5l-1.5 1.5a3.536 3.536 0 1 1 -5 -5l1.5 -1.5z" /><path d="M17 12l-5 -5l1.5 -1.5a3.536 3.536 0 1 1 5 5l-1.5 1.5z" />
    <path d="M3 21l2.5 -2.5" /><path d="M18.5 5.5l2.5 -2.5" /><path d="M10 11l-2 2" /><path d="M13 14l-2 2" />
  </Svg>
);
/** Run (execute a check, resume a pipeline): the play triangle Workspace uses for "Run" (Tabler `player-play`). */
export const IconRun = () => (
  <Svg><path d="M7 4v16l13 -8z" /></Svg>
);
/** Pipeline row: open this connector's metrics on Monitoring (a small pulse line, matching the Monitoring nav icon). */
export const IconMetrics = () => (
  <Svg><path d="M3 12h4l3-8 4 16 3-8h4" /></Svg>
);
export const IconCheck = () => (
  <Svg><path d="M5 12l5 5l10 -10" /></Svg>
);
/** Deploy: publish — the mirror of Workspace's download arrow, so it never looks like Run (Resume) beside it. */
export const IconDeploy = () => (
  <Svg><path d="M12 21V9" /><path d="m7 14 5-5 5 5" /><path d="M5 3h14" /></Svg>
);
export const IconPause = () => (
  <Svg><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></Svg>
);
export const IconResume = IconRun;
/** Restart: Workspace's refresh arrows. */
export const IconRestart = () => (
  <Svg><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16" /><path d="M3 21v-5h5" /><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8" /><path d="M21 3v5h-5" /></Svg>
);
/** Three dots: the row's "More actions" menu. */
export const IconMore = () => (
  <Svg><circle cx="5" cy="12" r="1.2" /><circle cx="12" cy="12" r="1.2" /><circle cx="19" cy="12" r="1.2" /></Svg>
);
export const IconSearch = () => (
  <Svg><circle cx="10" cy="10" r="7" /><path d="M21 21l-6 -6" /></Svg>
);
// Distinct from IconView (a plain eye, used for "view this record") and
// IconSearch (a plain lens, used for filter/search boxes) — a lens finding
// something new, for the "scan the source for tables/schemas" action.
export const IconDiscover = () => (
  <Svg16><circle cx="6.5" cy="6.5" r="4.5" /><path d="M9.8 9.8L14 14" /><path d="M6.5 4.5v4M4.5 6.5h4" /></Svg16>
);
// Distinct from IconView (an eye, "view this record") — angle brackets for
// "preview the generated config/code".
export const IconPreview = () => (
  <Svg16><path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5M9 3l-2 10" /></Svg16>
);
export const IconExternal = () => (
  <svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M6 3h7v7M13 3l-8 8" stroke="#fff" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
);
