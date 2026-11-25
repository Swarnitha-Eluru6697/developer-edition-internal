/**
 * Add / edit an alert channel — a right-hand slide-over like every other form in the Workspace (never a centred dialog).
 * The webhook URL is typed here and never shown again: editing a channel shows only its hint, and leaving the field blank keeps the
 * stored URL.
 */
import { useState } from "react";
import { useDispatch } from "react-redux";
import { Drawer, Field } from "../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../shared/ui/drawerStyles";
import SearchSelect from "../../shared/ui/SearchSelect";
import { notify } from "../../store/uiSlice";
import type { AppDispatch } from "../../store";
import type { AlertChannel, AlertChannelKind, AlertSeverity } from "../../types";
import { useCreateAlertChannelMutation, useUpdateAlertChannelMutation } from "./api";
import { errorField, errorText } from "./errors";
import { SEVERITIES, SEVERITY_FLOOR } from "./kinds";
import { Toggle } from "./ui";
import type { JobChoice } from "./useJobChoices";
import { JOB_KIND_LABEL } from "./kinds";

const KINDS: { kind: AlertChannelKind; label: string; icon: string; hint: string; placeholder: string }[] = [
  { kind: "slack", label: "Slack", icon: "ti-brand-slack", hint: "An Incoming Webhook URL from hooks.slack.com.", placeholder: "https://hooks.slack.com/services/T000/B000/XXXX" },
  { kind: "teams", label: "Microsoft Teams", icon: "ti-brand-windows", hint: "The webhook URL of a Teams channel connector or Workflow.", placeholder: "https://…webhook.office.com/webhookb2/…" },
];

interface Props {
  /** null = add a new channel. */
  channel: AlertChannel | null;
  jobs: JobChoice[];
  onClose: () => void;
}

export function ChannelDrawer({ channel, jobs, onClose }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const editing = !!channel;
  const [kind, setKind] = useState<AlertChannelKind>(channel?.kind ?? "slack");
  const [name, setName] = useState(channel?.name ?? "");
  const [url, setUrl] = useState("");
  const [severity, setSeverity] = useState<AlertSeverity>(channel?.min_severity ?? "low");
  const [scope, setScope] = useState(channel?.scope_job_key ?? "");
  const [enabled, setEnabled] = useState(channel?.enabled ?? true);
  const [error, setError] = useState<{ field: string | null; message: string } | null>(null);
  const [create, { isLoading: creating }] = useCreateAlertChannelMutation();
  const [update, { isLoading: updating }] = useUpdateAlertChannelMutation();
  const busy = creating || updating;
  const info = KINDS.find((k) => k.kind === kind)!;
  const needsUrl = !editing || kind !== channel?.kind;                  // a new channel, or a new type (the old URL belongs to the old type)
  const blocked = busy || !name.trim() || (needsUrl && !url.trim());
  const scopeJob = jobs.find((j) => j.key === scope);
  const field = (f: string) => (error?.field === f ? error.message : null);

  const save = async () => {
    if (blocked) return;
    setError(null);
    const common = { name: name.trim(), kind, min_severity: severity, enabled, scope_job_key: scope || null, scope_job_name: scopeJob?.name ?? null };
    try {
      if (channel) {
        await update({ id: channel.id, body: { ...common, scope_job_key: scope, ...(url.trim() ? { webhook_url: url.trim() } : {}) } }).unwrap();
        dispatch(notify({ kind: "success", message: `Channel "${common.name}" saved` }));
      } else {
        await create({ ...common, webhook_url: url.trim() }).unwrap();
        dispatch(notify({ kind: "success", message: `Channel "${common.name}" added` }));
      }
      onClose();
    } catch (e) {
      setError({ field: errorField(e), message: errorText(e, "Could not save the channel") });
    }
  };

  const errorLine = (f: string) => field(f) && <div role="alert" style={{ fontSize: 11.5, color: "#B23B3B", marginTop: 4 }}>{field(f)}</div>;

  return (
    <Drawer
      title={editing ? `Edit ${channel.name}` : "Add alert channel"}
      icon="pi-bell"
      onClose={onClose}
      busy={busy}
      footer={(
        <>
          <button onClick={onClose} disabled={busy} style={drawerBtn(false)}>Cancel</button>
          <button onClick={save} disabled={blocked} style={{ ...drawerBtn(true), marginLeft: "auto", opacity: blocked ? 0.5 : 1, cursor: blocked ? "not-allowed" : "pointer" }}>
            {busy ? "Saving…" : editing ? "Save changes" : "Add channel"}
          </button>
        </>
      )}
    >
      <p style={{ fontSize: 12, color: "#5b6d62", margin: "0 0 14px", lineHeight: 1.5 }}>
        Alerts matching the filters below are posted to this channel. Everything else is left alone.
      </p>

      <Field label="Channel type">
        <div role="group" aria-label="Channel type" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {KINDS.map((k) => (
            <button key={k.kind} type="button" aria-pressed={kind === k.kind} onClick={() => setKind(k.kind)}
              style={{
                display: "flex", alignItems: "center", justifyContent: "center", gap: 7, height: 34, borderRadius: 8, fontSize: 12.5, cursor: "pointer",
                background: kind === k.kind ? "#E3F6EA" : "#fff", color: kind === k.kind ? "#086347" : "#2E3D36", fontWeight: kind === k.kind ? 600 : 500,
                border: kind === k.kind ? "1px solid #0A7D5A" : "1px solid #E3E8E2",
              }}>
              <i className={`ti ${k.icon}`} style={{ fontSize: 15 }} />{k.label}
            </button>
          ))}
        </div>
        <div style={{ fontSize: 11, color: "#73827a", marginTop: 5 }}>{info.hint}</div>
      </Field>

      <Field label="Name" required hint="How this channel is identified on this page. Must be unique.">
        <input aria-label="Name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. data-oncall" style={inp} maxLength={120} />
        {errorLine("name")}
      </Field>

      <Field
        label={editing && !needsUrl ? "Webhook URL (leave blank to keep the current one)" : "Webhook URL"}
        required={needsUrl}
        hint={editing && channel
          ? `Currently ${channel.url_hint}. The full URL is never shown again after it is saved: anyone holding it can post into your channel.`
          : "Must be https://. Stored encrypted and never returned by the API."}
      >
        <input aria-label="Webhook URL" type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={info.placeholder}
          autoComplete="off" spellCheck={false} style={{ ...inp, fontFamily: "var(--mono)" }} />
        {errorLine("webhook_url")}
      </Field>

      <Field label="Send alerts of severity">
        <SearchSelect ariaLabel="Severity" noun="severity" searchable={false} value={severity} onChange={(v) => setSeverity(v as AlertSeverity)}
          options={SEVERITIES.map((s) => ({ value: s, label: SEVERITY_FLOOR[s] }))} />
      </Field>

      <Field label="For job" hint="Pick one job to send only its alerts here. Other jobs' alerts will not reach this channel.">
        <SearchSelect ariaLabel="For job" noun="job" value={scope} onChange={setScope}
          options={[{ value: "", label: "All jobs" }, ...jobs.map((j) => ({ value: j.key, label: j.name, hint: JOB_KIND_LABEL[j.kind] }))]} />
      </Field>

      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 12px", border: "0.5px solid #E3E8E2", borderRadius: 8, background: "#F8FAF9" }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12.5, fontWeight: 600 }}>Enabled</div>
          <div style={{ fontSize: 11, color: "#73827a" }}>Turn off to stop delivery without losing the configuration.</div>
        </div>
        <Toggle checked={enabled} onChange={setEnabled} label="Enabled" />
      </div>
      {error && !error.field && <div role="alert" style={{ fontSize: 12, color: "#B23B3B", marginTop: 12 }}>{error.message}</div>}
    </Drawer>
  );
}
