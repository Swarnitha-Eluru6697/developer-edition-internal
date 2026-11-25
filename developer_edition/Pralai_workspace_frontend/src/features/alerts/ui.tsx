/** Small pieces the alerts screens share: chips and the on/off switch. */
import type { CSSProperties, ReactNode } from "react";
import type { AlertSeverity } from "../../types";

type Tone = "red" | "amber" | "slate" | "blue" | "green" | "line";
const TONES: Record<Tone, CSSProperties> = {
  red: { background: "#FBE9E9", color: "#B23B3B", border: "1px solid transparent" },
  amber: { background: "#FDEBDD", color: "#A8470A", border: "1px solid transparent" },
  slate: { background: "#EEF2ED", color: "#4B5A53", border: "1px solid transparent" },
  blue: { background: "#DBEAFE", color: "#1D4ED8", border: "1px solid transparent" },
  green: { background: "#EAF3E1", color: "#2F6B14", border: "1px solid transparent" },
  line: { background: "#fff", color: "#4B5A53", border: "1px solid #E3E8E2" },
};

export function Chip({ tone = "line", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} style={{
      display: "inline-flex", alignItems: "center", gap: 4, padding: "1px 9px", borderRadius: 999, fontSize: 11.5, fontWeight: 600,
      whiteSpace: "nowrap", ...TONES[tone],
    }}>{children}</span>
  );
}

const SEVERITY_TONE: Record<AlertSeverity, Tone> = { high: "red", medium: "amber", low: "slate" };
export function SeverityChip({ severity }: { severity: AlertSeverity }) {
  return <Chip tone={SEVERITY_TONE[severity]}>{severity}</Chip>;
}

/** An on/off switch (the Agents Rules table's). Always carries a label for screen readers. */
export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (on: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} title={label} disabled={disabled}
      onClick={() => onChange(!checked)}
      style={{
        width: 34, height: 20, borderRadius: 999, border: "none", padding: 2, flexShrink: 0, cursor: disabled ? "not-allowed" : "pointer",
        background: checked ? "#0A7D5A" : "#CFD8D1", opacity: disabled ? 0.5 : 1, display: "flex", alignItems: "center",
        justifyContent: checked ? "flex-end" : "flex-start", transition: "background .12s",
      }}>
      <span style={{ width: 16, height: 16, borderRadius: "50%", background: "#fff", boxShadow: "0 1px 2px rgba(0,0,0,.25)" }} />
    </button>
  );
}
