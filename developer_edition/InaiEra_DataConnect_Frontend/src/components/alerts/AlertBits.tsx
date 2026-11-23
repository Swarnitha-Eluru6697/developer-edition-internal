/* Small pieces the Alerts page shares: the severity / state badges and the on/off switch. */
import { Pill } from "../ui";
import type { AlertSeverity, AlertState } from "../../types/api";

const SEVERITY_CLASS: Record<AlertSeverity, string> = { high: "br", medium: "ba", low: "bgr" };
export function SeverityPill({ severity }: { severity: AlertSeverity }) {
  return <Pill cls={SEVERITY_CLASS[severity]}>{severity}</Pill>;
}

const STATE_CLASS: Record<AlertState, string> = { open: "br", acknowledged: "bb", resolved: "bg" };
export function AlertStatePill({ state }: { state: AlertState }) {
  return <Pill cls={STATE_CLASS[state]}>{state}</Pill>;
}

/** An on/off switch (the Workspace Rules table's). Always carries a label for screen readers. */
export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (on: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} title={label} disabled={disabled}
      className={`al-switch${checked ? " on" : ""}`} onClick={() => onChange(!checked)}>
      <span />
    </button>
  );
}
