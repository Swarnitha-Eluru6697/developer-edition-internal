/**
 * Shared geometry and colours for the dbt surface.
 *
 * One gutter and one row rhythm for every list here, and both lists are grids
 * whose header and rows share `gridRow(cols)` so they can't drift apart: a row
 * used to change height when you clicked from the project list into a
 * project, and the flex-based runs list repacked its values with content
 * length instead of holding a column. COL_GAP is the only distance between
 * columns; equal `minmax(0,…fr)` tracks spread the slack evenly (a single
 * flexible track swallowed every spare pixel), the 0 floor lets a track shrink
 * below its content, and the trailing fixed track keeps the chevron off the edge.
 */
import type { CSSProperties } from "react";

export const STATUS_COLOR: Record<string, string> = {
  succeeded: "var(--ws-ok, #2f6b14)", failed: "var(--ws-bad, #b23b3b)", running: "var(--ws-accent, #0A7D5A)",
  queued: "var(--ws-warn, #a8470a)", cancelling: "var(--ws-warn, #a8470a)", cancelled: "var(--ws-subtle, #73827a)",
};
export const STATUS_BG: Record<string, string> = {
  succeeded: "var(--ws-ok-bg, #eaf3e1)", failed: "var(--ws-bad-bg, #fbe9e9)", running: "var(--ws-accent-bg, #E3F6EA)",
  queued: "var(--ws-warn-bg, #fdebdd)", cancelling: "var(--ws-warn-bg, #fdebdd)", cancelled: "var(--ws-fill, #eef2ed)",
};

export const GUTTER = 16;
export const ROW_PAD = `10px ${GUTTER}px`;
export const HEAD_PAD = `7px ${GUTTER}px`;
export const EMPTY_PAD = `32px ${GUTTER}px`;
export const COL_GAP = 16;

export const PROJECT_COLS = "minmax(0,1.8fr) repeat(3, minmax(0,1fr)) 56px";
export const RUN_COLS = "repeat(5, minmax(0,1fr)) 24px";

/** Grid geometry shared by a list's header and its rows. */
export const gridRow = (cols: string): CSSProperties => ({
  display: "grid", gridTemplateColumns: cols, columnGap: COL_GAP, alignItems: "center",
});

export const HEAD_STYLE: CSSProperties = {
  padding: HEAD_PAD, fontSize: 10.5, color: "var(--ws-subtle, #73827a)", textTransform: "uppercase",
  letterSpacing: ".06em", borderBottom: "0.5px solid var(--ws-line, #E3E8E2)", background: "var(--ws-thead, #F8FAF9)",
};

export const primaryBtn: CSSProperties = {
  height: 28, padding: "0 11px", fontSize: 12, background: "var(--ws-primary, #0A7D5A)", color: "var(--ws-on-primary, #fff)",
  border: "none", borderRadius: 6, cursor: "pointer", display: "flex", alignItems: "center", gap: 5,
};
