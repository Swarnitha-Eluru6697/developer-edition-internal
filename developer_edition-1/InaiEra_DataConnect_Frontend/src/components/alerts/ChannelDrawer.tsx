/*
 * Add / edit an alert channel — the right-hand drawer like every other form here (never a centred dialog). The webhook URL is typed here and never
 * shown again: editing a channel shows only its hint, and leaving the field blank keeps the stored URL.
 */
import { useState } from "react";
import { useApp } from "../../context/AppContext";
import { firstFieldError } from "../../errors";
import { useCreateAlertChannelMutation, useUpdateAlertChannelMutation } from "../../store/apiSlice";
import type { AlertChannel, AlertChannelKind, AlertSeverity } from "../../types/api";
import { DIRECTION_LABEL, SEVERITIES, SEVERITY_FLOOR } from "../../alertWords";
import Select from "../Select";
import { Drawer, FieldError } from "../ui";
import { Toggle } from "./AlertBits";
import type { PipelineChoice } from "./RuleDrawer";

const KINDS: { kind: AlertChannelKind; label: string; hint: string; placeholder: string }[] = [
  { kind: "slack", label: "Slack", hint: "An Incoming Webhook URL from hooks.slack.com.", placeholder: "https://hooks.slack.com/services/T000/B000/XXXX" },
  { kind: "teams", label: "Microsoft Teams", hint: "The webhook URL of a Teams Workflow (\"Send webhook alerts to a channel\") or channel connector.",
    placeholder: "https://…logic.azure.com/workflows/…  or  https://…webhook.office.com/…" },
];

export function ChannelDrawer({ channel, pipelines, onClose }: { channel: AlertChannel | null; pipelines: PipelineChoice[]; onClose: () => void }) {
  const { ok, err } = useApp();
  const [kind, setKind] = useState<AlertChannelKind>(channel?.kind ?? "slack");
  const [name, setName] = useState(channel?.name ?? "");
  const [url, setUrl] = useState("");
  const [severity, setSeverity] = useState<AlertSeverity>(channel?.min_severity ?? "low");
  const [scope, setScope] = useState(channel?.scope_pipeline_id ?? "");
  const [enabled, setEnabled] = useState(channel?.enabled ?? true);
  const [fieldError, setFieldError] = useState<{ field: string; message: string } | null>(null);
  const [create, { isLoading: creating }] = useCreateAlertChannelMutation();
  const [update, { isLoading: updating }] = useUpdateAlertChannelMutation();
  const busy = creating || updating;
  const info = KINDS.find((k) => k.kind === kind)!;
  const needsUrl = !channel || kind !== channel.kind;             // a new channel, or a new type (the stored URL belongs to the old type)
  const blocked = busy || !name.trim() || (needsUrl && !url.trim());
  const scopePipeline = pipelines.find((p) => p.id === scope);
  const errorFor = (f: string) => (fieldError?.field === f ? <FieldError id={`channel-${f}-error`} message={fieldError.message} /> : null);

  const save = async () => {
    if (blocked) return;
    setFieldError(null);
    const common = { name: name.trim(), kind, min_severity: severity, enabled, scope_pipeline_name: scopePipeline?.name ?? null };
    try {
      if (channel) {
        await update({ id: channel.id, body: { ...common, scope_pipeline_id: scope, ...(url.trim() ? { webhook_url: url.trim() } : {}) } }).unwrap();
        ok(`Channel "${common.name}" saved`);
      } else {
        await create({ ...common, scope_pipeline_id: scope || null, webhook_url: url.trim() }).unwrap();
        ok(`Channel "${common.name}" added`);
      }
      onClose();
    } catch (e) {
      const fe = firstFieldError(e);
      if (fe) setFieldError({ field: fe.field, message: fe.message });
      else err(e);
    }
  };

  return (
    <Drawer
      title={channel ? `Edit ${channel.name}` : "Add alert channel"}
      onClose={onClose}
      footer={(
        <>
          <button type="button" className="btn-s" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="button" className="btn-p" style={{ marginLeft: "auto" }} onClick={save} disabled={blocked}>
            {busy ? "Saving…" : channel ? "Save changes" : "Add channel"}
          </button>
        </>
      )}
    >
      <div className="al-form">
        <p className="ab" style={{ marginBottom: 14 }}>Alerts matching the filters below are posted to this channel. Everything else is left alone.</p>

        <div className="fld">
          <label id="channel-type-label">Channel type</label>
          <div role="group" aria-labelledby="channel-type-label" className="al-kinds">
            {KINDS.map((k) => (
              <button key={k.kind} type="button" aria-pressed={kind === k.kind} className={kind === k.kind ? "on" : undefined} onClick={() => setKind(k.kind)}>
                {k.label}
              </button>
            ))}
          </div>
          <div className="ab" style={{ marginTop: 5 }}>{info.hint}</div>
        </div>

        <div className="fld">
          <label htmlFor="channel-name">Name *</label>
          <input id="channel-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. data-oncall" maxLength={120}
            aria-invalid={fieldError?.field === "name"} />
          <div className="ab" style={{ marginTop: 4 }}>How this channel is identified on this page. Must be unique.</div>
          {errorFor("name")}
        </div>

        <div className="fld">
          <label htmlFor="channel-url">{channel && !needsUrl ? "Webhook URL (leave blank to keep the current one)" : "Webhook URL *"}</label>
          <input id="channel-url" type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={info.placeholder} autoComplete="off"
            spellCheck={false} className="mono" aria-invalid={fieldError?.field === "webhook_url"} />
          <div className="ab" style={{ marginTop: 4 }}>
            {channel ? `Currently ${channel.url_hint}. The full URL is never shown again after it is saved: anyone holding it can post into your channel.`
              : "Must be https://. Stored encrypted and never returned by the API."}
          </div>
          {errorFor("webhook_url")}
        </div>

        <div className="fld">
          <label htmlFor="channel-severity">Send alerts of severity</label>
          <Select id="channel-severity" value={severity} onChange={(e) => setSeverity(e.target.value as AlertSeverity)}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{SEVERITY_FLOOR[s]}</option>)}
          </Select>
        </div>

        <div className="fld">
          <label htmlFor="channel-scope">For pipeline</label>
          <Select id="channel-scope" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="">All pipelines</option>
            {pipelines.map((p) => <option key={p.id} value={p.id}>{`${p.name} (${DIRECTION_LABEL[p.direction] ?? p.direction})`}</option>)}
          </Select>
          <div className="ab" style={{ marginTop: 4 }}>Pick one pipeline to send only its alerts here. Other pipelines' alerts will not reach this channel.</div>
        </div>

        <div className="al-enabled">
          <div>
            <div className="al-enabled-t">Enabled</div>
            <div className="ab">Turn off to stop delivery without losing the configuration.</div>
          </div>
          <Toggle checked={enabled} onChange={setEnabled} label="Enabled" />
        </div>
      </div>
    </Drawer>
  );
}
