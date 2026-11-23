/**
 * The codegen agent card (Settings → Engines): which model answers the ⌘K assistant.
 *
 * "Codegen Agent (default)" is the self-hosted model behind the in-perimeter gateway. A holder of workspace:settings:manage may pick OpenAI or
 * Anthropic and enter their own API key; the key is checked live with that provider before it is saved, kept only in the secrets
 * store, and never shown again (only a masked form). From then on every assistant answer comes from that provider. Everyone else
 * sees which agent is active and cannot change it.
 *
 * The server says which agents may be chosen (available_providers): without the self-hosted gateway (Developer Edition) only
 * Anthropic and OpenAI are offered, and the assistant answers once a key is saved.
 */
import { useEffect, useState } from "react";
import { apiErrorMessage } from "../../../api/errors";
import type { CodegenProvider, EngineStatus } from "../../../types";
import { STATE } from "../engineState";
import { useGetCodegenAgentConfigQuery, useUpdateCodegenAgentConfigMutation, useVerifyCodegenKeyMutation } from "../api";
import SearchSelect from "../../../shared/ui/SearchSelect";

const OPTIONS: { value: CodegenProvider; label: string }[] = [
  { value: "codegen", label: "Codegen Agent (default)" },
  { value: "anthropic", label: "Anthropic key" },
  { value: "openai", label: "OpenAI key" },
];
const NAME: Record<CodegenProvider, string> = { codegen: "Codegen Agent (default)", anthropic: "Anthropic", openai: "OpenAI" };

type Outcome = { kind: "idle" } | { kind: "verifying" } | { kind: "success" | "error" | "info"; message: string };

export function CodegenAgentCard({ engine: e, onRefetchEngines }: { engine: EngineStatus; onRefetchEngines: () => void }) {
  const { data: config, refetch: refetchConfig } = useGetCodegenAgentConfigQuery();
  const [updateAgent, { isLoading: updating }] = useUpdateCodegenAgentConfigMutation();
  const [verifyKey, { isLoading: verifying }] = useVerifyCodegenKeyMutation();

  const [selected, setSelected] = useState<CodegenProvider | undefined>(undefined);
  const [keyInput, setKeyInput] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>({ kind: "idle" });

  // Follow what the server says is active (on load, and after every save).
  useEffect(() => {
    if (!config) return;
    setSelected(config.provider);
    if (config.status === "connected") setOutcome({ kind: "success", message: config.message || "Successfully connected" });
    else if (config.status === "unreachable") setOutcome({ kind: "error", message: config.message || "Connection failed" });
    else if (config.status === "not_configured" && config.message) setOutcome({ kind: "info", message: config.message });
  }, [config]);

  // Only the agents this deployment offers; an older server that does not say offers all three.
  const allowed = config?.available_providers;
  const options = allowed ? OPTIONS.filter((o) => allowed.includes(o.value)) : OPTIONS;
  const hasSelfHosted = options.some((o) => o.value === "codegen");

  const canEdit = config?.can_edit ?? false;
  const external = selected === "anthropic" || selected === "openai";
  const savedForSelected = !!config && config.provider === selected && config.has_key;
  const pending = updating || verifying;
  const typed = keyInput.trim();
  const canSubmit = canEdit && !pending && (selected === "codegen" ? config?.provider !== "codegen" : !!typed || savedForSelected);
  const st = STATE[e.status];

  const choose = (next: CodegenProvider) => {
    setSelected(next);
    setKeyInput("");
    setOutcome({ kind: "idle" });
  };

  const submit = async () => {
    setOutcome({ kind: "verifying" });
    try {
      if (!selected) return;
      if (selected === "codegen" || typed) {
        const res = await updateAgent({ provider: selected, api_key: selected === "codegen" ? undefined : typed }).unwrap();
        setOutcome({ kind: "success", message: res.message || "Successfully connected" });
        setKeyInput("");
      } else {
        const res = await verifyKey({ provider: selected }).unwrap();            // re-check the key already saved
        setOutcome(res.success ? { kind: "success", message: res.message || "Successfully connected" }
          : { kind: "error", message: res.message || "Connection failed" });
        if (!res.success) return;
      }
      onRefetchEngines();
      refetchConfig();
    } catch (err) {
      setOutcome({ kind: "error", message: apiErrorMessage(err, "Connection failed", { prefer: "detail" }) });
    }
  };

  return (
    <div style={{ background: "#fff", border: "1px solid #e8eaee", borderRadius: 12, padding: "18px 20px", boxShadow: "0 1px 2px rgba(16,24,40,.04)", display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: "#111827", flex: 1, minWidth: 0 }}>{e.name}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 500, color: st.text, whiteSpace: "nowrap" }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%", background: st.dot }} />
          {st.label}
        </div>
      </div>
      <div style={{ fontSize: 12.5, color: "#6b7280", lineHeight: 1.5 }}>{e.description}</div>
      {(e.detail || e.latency_ms != null) && (
        <div style={{ fontSize: 12, color: "#4b5563", display: "flex", flexDirection: "column", gap: 3 }}>
          {e.latency_ms != null && e.status === "connected" && <span>Responded in {e.latency_ms} ms</span>}
          {e.detail && <span style={{ color: e.status === "unreachable" ? "#b91c1c" : "#4b5563", wordBreak: "break-word" }}>{e.detail}</span>}
        </div>
      )}

      <div style={{ marginTop: 4, paddingTop: 10, borderTop: "1px solid #f1f3f5", display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <label htmlFor="codegen-agent" style={{ fontSize: 12, fontWeight: 600, color: "#374151" }}>Select Agent</label>
          {config && (
            <span style={{ fontSize: 11, fontWeight: 500, color: "#6b7280" }}>
              Active: <strong style={{ color: "#111827" }}>{NAME[config.provider]}</strong>
            </span>
          )}
        </div>

        <SearchSelect id="codegen-agent" value={selected ?? ""} onChange={(v) => choose(v as CodegenProvider)} disabled={pending || !canEdit || !selected}
          options={options} noun="agent" />

        {config && !canEdit && (
          <div style={{ fontSize: 11.5, color: "#6b7280" }}>You need permission to edit Workspace settings to change the agent.</div>
        )}

        {canEdit && external && (
          <>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 2 }}>
              <div style={{ position: "relative", flex: 1 }}>
                <input
                  type={showKey ? "text" : "password"} aria-label={`${NAME[selected!]} API key`} autoComplete="off" spellCheck={false}
                  placeholder={savedForSelected && !keyInput ? `•••••••• (${config?.masked_key ?? "key saved"})`
                    : selected === "openai" ? "Enter OpenAI key (sk-...)" : "Enter Anthropic key (sk-ant-...)"}
                  value={keyInput} onChange={(ev) => { setKeyInput(ev.target.value); if (outcome.kind !== "idle") setOutcome({ kind: "idle" }); }}
                  disabled={pending}
                  style={{ width: "100%", height: 34, padding: "0 32px 0 10px", fontSize: 12.5, fontFamily: showKey ? "monospace" : "inherit",
                    color: "#111827", background: "#fff", boxSizing: "border-box", outline: "none", borderRadius: 7,
                    border: `1px solid ${outcome.kind === "error" ? "#fca5a5" : outcome.kind === "success" ? "#86efac" : "#d1d5db"}` }}
                />
                <button type="button" onClick={() => setShowKey((v) => !v)} title={showKey ? "Mask key" : "Show key"}
                  style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", padding: 4, cursor: "pointer", color: "#9ca3af", display: "flex" }}>
                  <i className={`ti ti-eye${showKey ? "-off" : ""}`} style={{ fontSize: 14 }} />
                </button>
              </div>
              <button onClick={submit} disabled={!canSubmit}
                style={{ height: 34, padding: "0 16px", fontSize: 12.5, fontWeight: 600, color: "#fff", border: "none", borderRadius: 7, whiteSpace: "nowrap",
                  background: pending ? "#93c5fd" : "#2563eb", cursor: canSubmit ? "pointer" : "default", opacity: canSubmit || pending ? 1 : 0.6 }}>
                {pending ? "Verifying…" : "OK"}
              </button>
            </div>
            <div style={{ fontSize: 11.5, color: "#92400e", background: "#fffbeb", border: "1px solid #fde68a", borderRadius: 6, padding: "6px 9px", lineHeight: 1.5 }}>
              Your prompts and the table and column names the assistant looks up are sent to {NAME[selected!]}. The open file's content is
              still withheld unless your org's codegen policy allows it.{hasSelfHosted && " Switching back to the default removes the saved key."}
            </div>
          </>
        )}

        {canEdit && hasSelfHosted && selected === "codegen" && config?.provider !== "codegen" && (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", background: "#f8fafc", border: "1px solid #e2e8f0", borderRadius: 7, padding: "6px 10px" }}>
            <span style={{ fontSize: 12, color: "#64748b" }}>Switch back to the self-hosted agent</span>
            <button onClick={submit} disabled={!canSubmit}
              style={{ height: 28, padding: "0 12px", fontSize: 12, fontWeight: 600, background: "#2563eb", color: "#fff", border: "none", borderRadius: 6, cursor: canSubmit ? "pointer" : "default" }}>
              {pending ? "Connecting…" : "OK"}
            </button>
          </div>
        )}

        {outcome.kind === "verifying" && <Banner tone="info" text="Verifying with the provider…" />}
        {outcome.kind === "info" && <Banner tone="info" text={outcome.message} />}
        {outcome.kind === "success" && <Banner tone="ok" text={outcome.message} />}
        {outcome.kind === "error" && <Banner tone="bad" text={outcome.message} />}
      </div>
    </div>
  );
}

const TONES = {
  info: { fg: "#2563eb", bg: "#eff6ff", bd: "#bfdbfe", dot: "#2563eb" },
  ok: { fg: "#15803d", bg: "#f0fdf4", bd: "#bbf7d0", dot: "#22c55e" },
  bad: { fg: "#b91c1c", bg: "#fef2f2", bd: "#fecaca", dot: "#ef4444" },
} as const;

function Banner({ tone, text }: { tone: keyof typeof TONES; text: string }) {
  const t = TONES[tone];
  return (
    <div role="status" style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, fontWeight: 500, color: t.fg, background: t.bg, border: `1px solid ${t.bd}`, padding: "6px 10px", borderRadius: 6 }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: t.dot, flexShrink: 0 }} />
      <span style={{ wordBreak: "break-word" }}>{text}</span>
    </div>
  );
}
