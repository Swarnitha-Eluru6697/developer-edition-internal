/* One alert in the right-hand drawer: what happened, its history, and what you can do (open the pipeline, acknowledge, resolve). */
import { useState } from "react";
import { useApp } from "../../context/AppContext";
import { useAcknowledgeAlertMutation, useGetAlertQuery, useResolveAlertMutation } from "../../store/apiSlice";
import type { Alert, AlertEvent } from "../../types/api";
import { DIRECTION_LABEL, formatTs } from "../../alertWords";
import { Drawer, Pill } from "../ui";
import { AlertStatePill, SeverityPill } from "./AlertBits";

export function AlertDetailDrawer({ alert, canAct, onOpenPipeline, onClose }: {
  alert: Alert; canAct: boolean; onOpenPipeline: () => void; onClose: () => void;
}) {
  const { ok, err } = useApp();
  const { data: detail } = useGetAlertQuery(alert.id);
  const [ack, { isLoading: acking }] = useAcknowledgeAlertMutation();
  const [resolve, { isLoading: resolving }] = useResolveAlertMutation();
  const [note, setNote] = useState("");
  const a = detail ?? { ...alert, events: [] as AlertEvent[] };
  const busy = acking || resolving;

  const act = async (kind: "ack" | "resolve") => {
    try {
      await (kind === "ack" ? ack({ id: a.id, note: note.trim() || null }) : resolve({ id: a.id, note: note.trim() || null })).unwrap();
      ok(kind === "ack" ? "Alert acknowledged" : "Alert resolved");
      setNote("");
      if (kind === "resolve") onClose();
    } catch (e) { err(e); }
  };

  return (
    <Drawer
      title="Alert"
      onClose={onClose}
      footer={(
        <>
          <button type="button" className="btn-s" onClick={onOpenPipeline}>Open pipeline</button>
          {canAct && a.state === "open" && (
            <button type="button" className="btn-s" style={{ marginLeft: "auto" }} onClick={() => act("ack")} disabled={busy}>Acknowledge</button>
          )}
          {canAct && a.state !== "resolved" && (
            <button type="button" className="btn-p" style={a.state === "open" ? undefined : { marginLeft: "auto" }} onClick={() => act("resolve")} disabled={busy}>
              Resolve
            </button>
          )}
        </>
      )}
    >
      <div className="al-detail">
        <h2 className="mono">{a.rule_name}</h2>
        <div className="al-chips">
          <SeverityPill severity={a.severity} />
          <Pill cls="bgr">{a.pipeline_name}</Pill>
          <Pill cls="bgr">{DIRECTION_LABEL[a.direction] ?? a.direction}</Pill>
          <AlertStatePill state={a.state} />
        </div>

        <div className="fsec">What happened</div>
        <p className="al-summary">{a.summary}</p>
        <p className="tdm mono" style={{ padding: 0, marginTop: 6 }}>Raised {formatTs(a.raised_at)}</p>
        {a.delivery !== "sent" && a.state === "open" && (
          <p className="al-warn">
            {a.delivery === "failed" ? "No channel has received this yet: delivery failed." : "No channel has received this yet: none matches it."}
          </p>
        )}

        <div className="fsec">History</div>
        {a.events.length === 0 ? <div className="tdm" style={{ padding: 0 }}>Loading…</div> : (
          <ol className="al-history">
            {a.events.map((e, i) => (
              <li key={i}>
                <span aria-hidden className="al-dot" />
                <span>
                  <b>{e.action}</b> by {e.actor}<span className="tdm mono" style={{ padding: 0 }}> · {formatTs(e.at)}</span>
                  {e.note && <span className="al-note">{e.note}</span>}
                </span>
              </li>
            ))}
          </ol>
        )}

        {canAct && a.state !== "resolved" && (
          <div className="fld" style={{ marginTop: 14 }}>
            <label htmlFor="alert-note">Note (optional)</label>
            <input id="alert-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Shown in the history" maxLength={1000} />
          </div>
        )}
      </div>
    </Drawer>
  );
}
