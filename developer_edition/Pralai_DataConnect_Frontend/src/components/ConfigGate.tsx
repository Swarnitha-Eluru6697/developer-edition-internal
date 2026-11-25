/*
 * FE-GAP-01: renders its children only when the runtime configuration is usable. In FEDERATED mode that means the host supplied
 * `apiBase`; otherwise this renders a configuration error INSTEAD — before any store or request exists — so nothing can quietly
 * act against a development backend. (The tenant is not configuration: it comes from the signed ticket, see GET /me.) (The rule itself is src/runtimeConfig.ts.)
 *
 * `children` is a function so the configured part of the app (store, providers, pages) is not even constructed when the
 * configuration is bad.
 */
import React, { useEffect, type ReactNode } from "react";
import { configurationErrorMessage, resolveRuntimeConfig, type HostConfigField, type RuntimeConfig } from "../runtimeConfig";
import type { RuntimeMode } from "../runtimeMode";

type Resolved = Extract<RuntimeConfig, { ok: true }>;

export interface ConfigGateProps {
  mode: RuntimeMode;
  apiBase?: unknown;
  storedBase?: string | null;
  /** The host's error callback (DataConnectApp's `onError`): told once that DataConnect could not start. */
  onError?: (error: Error) => void;
  children: (config: Resolved) => ReactNode;
}

const FIELD_HELP: Record<HostConfigField, string> = {
  apiBase: "the same-origin tunnel route to the DataConnect backend",
};

function ConfigurationError({ missing, onError }: { missing: HostConfigField[]; onError?: (error: Error) => void }) {
  const message = configurationErrorMessage(missing);
  useEffect(() => {
    try { onError?.(new Error(message)); } catch { /* a faulty host callback must not take the message down too */ }
  }, [message, onError]);
  return (
    <div className="alert alert-w" role="alert" data-testid="config-error" style={{ margin: 13 }}>
      <div className="at">DataConnect is not configured</div>
      <div className="ab">
        The host application did not provide the settings DataConnect needs, so it has not started and made no requests.
        <ul style={{ margin: "8px 0 0 18px" }}>
          {missing.map((field) => (
            <li key={field}><code>{field}</code> — {FIELD_HELP[field]}</li>
          ))}
        </ul>
        <div style={{ marginTop: 8 }}>Pass them to DataConnect when mounting it, then reload.</div>
      </div>
    </div>
  );
}

export default function ConfigGate({ mode, apiBase, storedBase, onError, children }: ConfigGateProps) {
  const config = resolveRuntimeConfig({ mode, apiBase, storedBase });
  if (!config.ok) return <ConfigurationError missing={config.missing} onError={onError} />;
  return <>{children(config)}</>;
}
