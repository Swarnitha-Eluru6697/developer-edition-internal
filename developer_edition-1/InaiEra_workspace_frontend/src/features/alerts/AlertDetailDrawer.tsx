/** One alert in a right-hand drawer: what happened, its history, and what you can do about it (open the job, acknowledge, resolve). */
import { useState } from "react";
import { useDispatch } from "react-redux";
import { Drawer } from "../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../shared/ui/drawerStyles";
import { notify, showJob } from "../../store/uiSlice";
import type { AppDispatch } from "../../store";
import type { Alert } from "../../types";
import { useAcknowledgeAlertMutation, useGetAlertQuery, useResolveAlertMutation } from "./api";
import { errorText } from "./errors";
import { JOB_KIND_LABEL, JOB_SECTION } from "./kinds";
import { Chip, SeverityChip } from "./ui";
import { formatTs, mono } from "./format";

const STATE_TONE = { open: "red", acknowledged: "blue", resolved: "green" } as const;

interface Props {
  alert: Alert;
  canAct: boolean;
  onClose: () => void;
}

export function AlertDetailDrawer({ alert, canAct, onClose }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const { data: detail } = useGetAlertQuery(alert.id);
  const [ack, { isLoading: acking }] = useAcknowledgeAlertMutation();
  const [resolve, { isLoading: resolving }] = useResolveAlertMutation();
  const [note, setNote] = useState("");
  const a = detail ?? { ...alert, events: [] as { action: string; actor: string; note: string | null; at: string }[] };
  const busy = acking || resolving;

  const act = async (kind: "ack" | "resolve") => {
    try {
      await (kind === "ack" ? ack({ id: a.id, note: note.trim() }) : resolve({ id: a.id, note: note.trim() })).unwrap();
      dispatch(notify({ kind: "success", message: kind === "ack" ? "Alert acknowledged" : "Alert resolved" }));
      setNote("");
      if (kind === "resolve") onClose();
    } catch (e) {
      dispatch(notify({ kind: "error", message: errorText(e, "Could not update the alert") }));
    }
  };
  const openJob = () => { dispatch(showJob({ section: JOB_SECTION[a.job_kind], id: a.job_key })); onClose(); };

  return (
    <Drawer
      title="Alert"
      icon="pi-bell"
      onClose={onClose}
      busy={busy}
      width={460}
      footer={(
        <>
          <button onClick={openJob} style={drawerBtn(false)}><i className="ti ti-external-link" style={{ fontSize: 13 }} />Open job</button>
          {canAct && a.state === "open" && (
            <button onClick={() => act("ack")} disabled={busy} style={{ ...drawerBtn(false), marginLeft: "auto" }}>Acknowledge</button>
          )}
          {canAct && a.state !== "resolved" && (
            <button onClick={() => act("resolve")} disabled={busy} style={{ ...drawerBtn(true), ...(a.state === "open" ? {} : { marginLeft: "auto" }) }}>Resolve</button>
          )}
        </>
      )}
    >
      <h2 style={{ ...mono, fontSize: 15, margin: "0 0 8px", overflowWrap: "anywhere" }}>{a.rule_name}</h2>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 16 }}>
        <SeverityChip severity={a.severity} />
        <Chip title={`${JOB_KIND_LABEL[a.job_kind] ?? a.job_kind} job`}>{a.job_name}</Chip>
        <Chip tone={STATE_TONE[a.state]}>{a.state}</Chip>
      </div>

      <Section title="What happened">
        <p style={{ fontSize: 13.5, lineHeight: 1.55, margin: 0, overflowWrap: "anywhere" }}>{a.summary}</p>
        <p style={{ ...mono, fontSize: 11.5, color: "#9ca3af", margin: "8px 0 0" }}>Raised {formatTs(a.raised_at)}</p>
        {a.delivery !== "sent" && a.state === "open" && (
          <p style={{ fontSize: 12, color: "#B45309", margin: "8px 0 0" }}>
            {a.delivery === "failed" ? "No channel has received this yet: delivery failed." : "No channel has received this yet: none matches it."}
          </p>
        )}
      </Section>

      <Section title="History">
        {a.events.length === 0 && <div style={{ fontSize: 12, color: "#9ca3af" }}>Loading…</div>}
        <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {a.events.map((e, i) => (
            <li key={i} style={{ display: "flex", gap: 10, padding: "5px 0", fontSize: 12.5 }}>
              <span aria-hidden style={{ width: 8, height: 8, borderRadius: "50%", background: "#F97316", marginTop: 5, flexShrink: 0 }} />
              <span style={{ minWidth: 0 }}>
                <b>{e.action}</b> by {e.actor}<span style={{ ...mono, color: "#9ca3af" }}> · {formatTs(e.at)}</span>
                {e.note && <span style={{ display: "block", color: "#6b7280", overflowWrap: "anywhere" }}>{e.note}</span>}
              </span>
            </li>
          ))}
        </ol>
      </Section>

      {canAct && a.state !== "resolved" && (
        <Section title="Note (optional)">
          <input aria-label="Note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Shown in the history" maxLength={1000} style={inp} />
        </Section>
      )}
    </Drawer>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div style={{ fontSize: 10.5, fontWeight: 600, color: "#9ca3af", textTransform: "uppercase", letterSpacing: ".1em", marginBottom: 7 }}>{title}</div>
      {children}
    </div>
  );
}
