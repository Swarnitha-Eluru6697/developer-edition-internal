/*
 * FE-BUG-04: a structured failure display for DataConnect's own
 * infrastructure-specific errors — Kafka Connect rejected a config, a
 * source system timed out, a topic policy can't be applied, and so on —
 * instead of collapsing every one of them into a generic "Request failed"
 * toast. The call site supplies `title` (which action failed — this
 * component never guesses that) and the normalized FailureInfo (see
 * ../errorCopy.ts's toFailureInfo/toFailureInfoFromOperation); everything
 * else — the code-specific explanation, the raw backend message, whether
 * to offer Retry — is derived from that.
 */
import React, { useState } from "react";
import { Drawer } from "./ui";
import { explainCode, type FailureInfo } from "../errorCopy";

export interface FailureDialogProps extends FailureInfo {
  /** What failed, in the call site's own words — e.g. "Deploy failed". */
  title: string;
  /** Extra identifying context shown above the reason, e.g. { Connector: "PostgreSQL CDC" }. */
  context?: Record<string, string>;
  /** Only offered when both this and `retryable` are present — a code the
   * backend itself says is worth trying again is necessary, not sufficient:
   * the call site still has to supply how to actually retry it. */
  onRetry?: () => void;
  /** e.g. { label: "Edit pipeline", onClick: () => go("pipelineForm") } — shown when the
   * likely fix is changing something the call site itself can navigate to. */
  onEdit?: { label: string; onClick: () => void };
  onClose: () => void;
}

export default function FailureDialog({
  title, code, message, retryable, requestId, context, onRetry, onEdit, onClose,
}: FailureDialogProps) {
  const [showDetails, setShowDetails] = useState(false);
  const explanation = explainCode(code);

  return (
    <Drawer
      title={title}
      onClose={onClose}
      footer={
        <>
          <button className="btn-s" onClick={() => setShowDetails((s) => !s)}>{showDetails ? "Hide details" : "View details"}</button>
          {onEdit && <button className="btn-s" onClick={onEdit.onClick}>{onEdit.label}</button>}
          {retryable && onRetry && <button className="btn-p" onClick={onRetry}>Retry</button>}
        </>
      }
    >
      {explanation && <div style={{ fontSize: 13, lineHeight: 1.5 }}>{explanation}</div>}
      {context && Object.keys(context).length > 0 && (
        <div style={{ marginTop: explanation ? 12 : 0 }}>
          {Object.entries(context).map(([k, v]) => (
            <div className="kv" key={k}><span className="kvk">{k}</span><span className="kvv">{v}</span></div>
          ))}
        </div>
      )}
      <div className="fsec" style={{ marginTop: 14 }}>Reason</div>
      <div style={{ fontSize: 12, lineHeight: 1.6, color: "var(--color-text-secondary)" }}>{message}</div>
      {showDetails && (
        <div style={{ marginTop: 14 }}>
          <div className="kv"><span className="kvk">Code</span><span className="kvv" style={{ fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 400 }}>{code || "—"}</span></div>
          {requestId && (
            <div className="kv"><span className="kvk">Request ID</span><span className="kvv" style={{ fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 400 }}>{requestId}</span></div>
          )}
          <div className="kv"><span className="kvk">Retryable</span><span className="kvv">{retryable ? "Yes" : "No"}</span></div>
        </div>
      )}
    </Drawer>
  );
}
