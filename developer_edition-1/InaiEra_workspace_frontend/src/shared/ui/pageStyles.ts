import type { CSSProperties } from "react";

/** Header action buttons: the orange primary and the white bordered "ghost". */
export const pageBtn = (variant: "primary" | "ghost" = "primary", disabled = false): CSSProperties => ({
  display: "inline-flex", alignItems: "center", gap: 7, padding: "9px 15px", borderRadius: 9,
  fontSize: 13, fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1,
  ...(variant === "primary"
    ? { background: "#F97316", color: "#fff", border: "none", boxShadow: "0 1px 2px rgba(16,32,58,.06), 0 8px 24px rgba(16,32,58,.06)" }
    : { background: "#fff", color: "#1F2937", border: "1px solid #E5E7EB" }),
});
