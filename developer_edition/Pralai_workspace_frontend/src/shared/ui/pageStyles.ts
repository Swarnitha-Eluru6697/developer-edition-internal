import type { CSSProperties } from "react";

/** Header action buttons: the amber primary and the white bordered "ghost". */
export const pageBtn = (variant: "primary" | "ghost" = "primary", disabled = false): CSSProperties => ({
  display: "inline-flex", alignItems: "center", gap: 7, padding: "9px 15px", borderRadius: 9,
  fontSize: 13, fontWeight: variant === "primary" ? 700 : 600, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1,
  ...(variant === "primary"
    ? { background: "#F2B134", color: "var(--ws-on-amber, #0B3B2E)", border: "1px solid #E5A82C", boxShadow: "0 1px 2px rgba(11,59,46,.06), 0 8px 24px rgba(11,59,46,.06)" }
    : { background: "var(--ws-surface, #fff)", color: "var(--ws-text, #14201B)", border: "1px solid var(--ws-line, #E3E8E2)" }),
});
