/** The Open / Acknowledged / Resolved lists: one card per alert, and (on Open) the note about alerts no channel has received. */
import type { Alert } from "../../types";
import { Chip, SeverityChip } from "./ui";
import { formatTs, mono } from "./format";
import { JOB_KIND_LABEL } from "./kinds";

interface Props {
  alerts: Alert[] | undefined;
  loading: boolean;
  emptyText: string;
  onOpen: (a: Alert) => void;
}

export function AlertCards({ alerts, loading, emptyText, onOpen }: Props) {
  if (loading && !alerts) return <div style={{ padding: 32, textAlign: "center", color: "#73827a", fontSize: 13 }}>Loading alerts…</div>;
  if (!alerts?.length) {
    return (
      <div style={{ padding: "40px 16px", textAlign: "center" }}>
        <i className="ti ti-bell-check" style={{ fontSize: 30, color: "#cfd8d1", display: "block", marginBottom: 10 }} />
        <div style={{ fontSize: 13.5, fontWeight: 500, marginBottom: 4 }}>Nothing here</div>
        <div style={{ fontSize: 12.5, color: "#5b6d62" }}>{emptyText}</div>
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {alerts.map((a) => (
        <button key={a.id} type="button" onClick={() => onOpen(a)} aria-label={`Open alert ${a.rule_name} for ${a.job_name}`}
          style={{
            display: "block", width: "100%", textAlign: "left", background: "#fff", border: "1px solid #E3E8E2", borderRadius: 12, padding: "13px 16px",
            cursor: "pointer", font: "inherit", color: "inherit", boxShadow: "0 1px 2px rgba(11,59,46,.04)",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#7FCFAE")}
          onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#E3E8E2")}>
          <span style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <SeverityChip severity={a.severity} />
            <b style={{ ...mono, fontSize: 12.5 }}>{a.rule_name}</b>
            <Chip title={`${JOB_KIND_LABEL[a.job_kind] ?? a.job_kind} job`}>{a.job_name}</Chip>
            {a.state === "acknowledged" && <Chip tone="blue">acknowledged</Chip>}
            <span style={{ ...mono, marginLeft: "auto", fontSize: 11.5, color: "#73827a" }}>{formatTs(a.raised_at)}</span>
          </span>
          <span style={{ display: "block", fontSize: 13.5, marginTop: 8, lineHeight: 1.5, overflowWrap: "anywhere" }}>{a.summary}</span>
          <span style={{ display: "block", fontSize: 11.5, color: "#73827a", marginTop: 6 }}>
            Last: <b style={{ color: "#5b6d62" }}>{a.last_action}</b> by {a.last_actor} · {formatTs(a.last_at)}
          </span>
        </button>
      ))}
    </div>
  );
}

/** Open alerts that no channel has received (none matches, or delivery failed): they are not listed until they are sent. */
export function UndeliveredNote({ count, onChannels }: { count: number; onChannels: () => void }) {
  if (!count) return null;
  return (
    <div role="status" style={{ border: "1px solid #7FCFAE", background: "#FDEBDD", borderRadius: 12, padding: "12px 16px", marginBottom: 12 }}>
      <b style={{ fontSize: 13 }}>{count} alert{count === 1 ? "" : "s"} reached no channel yet</b>
      <p style={{ fontSize: 12.5, color: "#5b6d62", margin: "4px 0 0", lineHeight: 1.5 }}>
        Either no channel matches the job, or delivery failed. They are not listed below until they are sent; the sweep retries a few times. Add a channel or check{" "}
        <button type="button" onClick={onChannels} style={{ background: "none", border: "none", padding: 0, font: "inherit", fontWeight: 700, color: "#086347", cursor: "pointer" }}>Channels</button>{" "}
        for a failing webhook.
      </p>
    </div>
  );
}
