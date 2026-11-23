/** Formatting and sizes the alerts screens share (kept out of ui.tsx so that file only exports components: Fast Refresh). */
import type { CSSProperties } from "react";

export const PAGE_SIZE = 10;

const pad = (n: number) => String(n).padStart(2, "0");
/** "2026-10-29 09:42" in the viewer's own time zone. */
export function formatTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export const mono: CSSProperties = { fontFamily: "var(--mono)" };

/** "Showing 11–20 of 34 alerts" */
export function showing(offset: number, shown: number, total: number, noun: string): string {
  if (!total) return `No ${noun}s`;
  return `Showing ${offset + 1}–${offset + shown} of ${total} ${noun}${total === 1 ? "" : "s"}`;
}
