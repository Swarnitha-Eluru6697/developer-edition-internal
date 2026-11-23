/**
 * The page title block every full-page view opens with — eyebrow, display-font
 * title, subtitle and right-aligned actions — matching the PageHead of the
 * other InAiEra apps (Agents, Unified Catalog, ML Studio).
 */
import type { ReactNode } from "react";

interface Props {
  eyebrow: string;
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}

export function PageHeader({ eyebrow, title, subtitle, actions }: Props) {
  return (
    <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 16, padding: "24px 28px 20px", background: "#F9FAFB", flexShrink: 0 }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".12em", color: "#C2410C", textTransform: "uppercase", marginBottom: 7 }}>{eyebrow}</div>
        <h1 style={{ fontFamily: "var(--disp)", fontSize: 23, fontWeight: 600, letterSpacing: "-.01em", color: "#1F2937", margin: 0 }}>{title}</h1>
        {subtitle && <div style={{ fontSize: 13.5, color: "#6B7280", marginTop: 6, maxWidth: "70ch" }}>{subtitle}</div>}
      </div>
      {actions && <div style={{ display: "flex", gap: 9, flex: "none" }}>{actions}</div>}
    </div>
  );
}
