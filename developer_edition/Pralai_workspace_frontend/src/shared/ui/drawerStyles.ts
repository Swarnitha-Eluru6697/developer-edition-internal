/**
 * Input and footer-button styles for forms rendered inside <Drawer>.
 *
 * Kept out of Drawer.tsx so that module exports only components
 * (react-refresh/only-export-components).
 */
import type { CSSProperties } from "react";

export const drawerInput: CSSProperties = {
  fontSize: 12, height: 30, border: "0.5px solid var(--ws-line-strong, #cfd8d1)", borderRadius: 6,
  padding: "0 8px", boxSizing: "border-box", width: "100%",
};

/** Footer button. `primary` is the submit action, filled blue. */
export function drawerBtn(primary: boolean): CSSProperties {
  return {
    height: 32, padding: "0 14px", fontSize: 12.5, borderRadius: 6, cursor: "pointer",
    display: "flex", alignItems: "center", justifyContent: "center", gap: 5,
    background: primary ? "var(--ws-primary, #0A7D5A)" : "var(--ws-surface, #fff)",
    color: primary ? "var(--ws-on-primary, #fff)" : "var(--ws-text-2, #2E3D36)",
    border: primary ? "none" : "0.5px solid var(--ws-line-strong, #cfd8d1)",
  };
}

/** Scrolls as usual (wheel, trackpad, keyboard) but draws no scrollbar. */
export const hiddenScrollbar: CSSProperties = { scrollbarWidth: "none", msOverflowStyle: "none" };
