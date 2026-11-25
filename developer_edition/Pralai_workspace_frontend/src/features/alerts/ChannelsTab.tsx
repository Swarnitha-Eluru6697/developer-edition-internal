/** Channels: where alerts are posted (Slack / Microsoft Teams webhooks). One card per channel with Send test, edit and remove. */
import { useDispatch } from "react-redux";
import { Glyph } from "../../shared/ui/Glyph";
import { askConfirm } from "../../shared/ui/dialogStore";
import { ACTION_ICON, IconAction } from "../../shared/ui/IconAction";
import { notify } from "../../store/uiSlice";
import type { AppDispatch } from "../../store";
import type { AlertChannel } from "../../types";
import { useDeleteAlertChannelMutation, useTestAlertChannelMutation } from "./api";
import { errorText } from "./errors";
import { SEVERITY_FLOOR_SHORT } from "./kinds";
import { Chip } from "./ui";
import { formatTs, mono } from "./format";

const KIND_LABEL = { slack: "Slack", teams: "Teams" } as const;

interface Props {
  channels: AlertChannel[] | undefined;
  loading: boolean;
  canManage: boolean;
  onEdit: (c: AlertChannel) => void;
}

export function ChannelsTab({ channels, loading, canManage, onEdit }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const [test, { isLoading: testing, originalArgs }] = useTestAlertChannelMutation();
  const [remove] = useDeleteAlertChannelMutation();

  const sendTest = async (c: AlertChannel) => {
    try {
      const r = await test(c.id).unwrap();
      dispatch(notify({ kind: r.ok ? "success" : "error", message: r.ok ? `Test message sent to ${c.name}` : `Test to ${c.name} failed: ${r.detail}` }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: errorText(e, "Could not send the test") }));
    }
  };
  const del = async (c: AlertChannel) => {
    if (!(await askConfirm({ title: "Remove channel?", message: `"${c.name}" will stop receiving alerts. The webhook itself is not touched.`, confirmLabel: "Remove", danger: true }))) return;
    try {
      await remove(c.id).unwrap();
      dispatch(notify({ kind: "success", message: `Removed channel "${c.name}"` }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: errorText(e, "Remove failed") }));
    }
  };

  if (loading && !channels) return <div style={{ padding: 32, textAlign: "center", color: "#73827a", fontSize: 13 }}>Loading channels…</div>;
  return (
    <div>
      <p style={{ fontSize: 12.5, color: "#5b6d62", margin: "0 0 12px", lineHeight: 1.5 }}>
        Alerts are posted to the channels below. A channel limited to one job receives only that job's alerts; a severity floor keeps quieter notices out of an on-call channel.
      </p>
      {channels?.length === 0 && (
        <div style={{ padding: "40px 16px", textAlign: "center" }}>
          <i className="ti ti-broadcast" style={{ fontSize: 30, color: "#cfd8d1", display: "block", marginBottom: 10 }} />
          <div style={{ fontSize: 13.5, fontWeight: 500, marginBottom: 4 }}>No channels yet</div>
          <div style={{ fontSize: 12.5, color: "#5b6d62" }}>Add a Slack or Microsoft Teams channel: alerts are only sent to channels, and wait here until there is one.</div>
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {channels?.map((c) => {
          const status = c.last_ok === null ? { tone: "slate" as const, text: "not used yet" } : c.last_ok ? { tone: "green" as const, text: "last send ok" } : { tone: "red" as const, text: "last send failed" };
          return (
            <div key={c.id} style={{ background: "#fff", border: "1px solid #E3E8E2", borderRadius: 12, padding: "13px 16px", opacity: c.enabled ? 1 : 0.65 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <b style={{ fontSize: 14 }}>{c.name}</b>
                <Chip>{KIND_LABEL[c.kind]}</Chip>
                <Chip tone={status.tone}>{status.text}</Chip>
                {!c.enabled && <Chip tone="slate">off</Chip>}
                {canManage && (
                  <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
                    <button type="button" onClick={() => sendTest(c)} disabled={testing && originalArgs === c.id} title={`Send a test message to ${c.name}`}
                      style={{ display: "inline-flex", alignItems: "center", gap: 6, height: 30, padding: "0 11px", fontSize: 12.5, fontWeight: 600, background: "#fff",
                        color: "#14201B", border: "1px solid #E3E8E2", borderRadius: 8, cursor: "pointer" }}>
                      <Glyph name="pi-test" size={14} />{testing && originalArgs === c.id ? "Sending…" : "Send test"}
                    </button>
                    <IconAction icon={ACTION_ICON.edit} label={`Edit ${c.name}`} onClick={() => onEdit(c)} />
                    <IconAction icon={ACTION_ICON.delete} label={`Remove ${c.name}`} danger onClick={() => del(c)} />
                  </div>
                )}
              </div>
              <p style={{ fontSize: 12.5, color: "#5b6d62", margin: "8px 0 0" }}>
                Receives <b>{SEVERITY_FLOOR_SHORT[c.min_severity]}</b> from <b>{c.scope_job_name ?? "all jobs"}</b> · <span style={mono}>{c.url_hint}</span>
              </p>
              <p style={{ fontSize: 12, color: "#73827a", margin: "4px 0 0" }}>
                {c.last_attempt_at ? <>Last attempt {formatTs(c.last_attempt_at)} — {c.last_detail ?? ""}</> : "No messages sent yet."}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
