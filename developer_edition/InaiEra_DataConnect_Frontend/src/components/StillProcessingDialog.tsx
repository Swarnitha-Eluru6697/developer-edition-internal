/*
 * FE-BUG-05: shown when the browser stops WATCHING a long-running action
 * before it reaches a terminal status — either it simply took longer than
 * this tab was willing to wait (`polling_timeout`), or a network hiccup
 * broke the polling loop itself (`network_disconnected`). Neither one means
 * the action failed; the backend may well still be working on it (or may
 * already have finished) — this is deliberately a different, calmer
 * component from FailureDialog, not a reskinned version of it, so the two
 * can never be confused with each other at a glance.
 */
import React from "react";
import { Drawer } from "./ui";

export interface StillProcessingDialogProps {
  /** e.g. "Deploy" — what's still being processed. */
  action: string;
  reason: "polling_timeout" | "network_disconnected";
  /** Extra identifying context, e.g. { Pipeline: "PostgreSQL CDC" }. */
  context?: Record<string, string>;
  /** Resumes watching — calling the same poll again, not resubmitting the action itself. */
  onContinueMonitoring: () => void;
  onClose: () => void;
}

const REASON_COPY: Record<StillProcessingDialogProps["reason"], string> = {
  polling_timeout: "is taking longer than expected. It is still being processed.",
  network_disconnected: "lost connection while this tab was checking on it. It may still be in progress.",
};

export default function StillProcessingDialog({
  action, reason, context, onContinueMonitoring, onClose,
}: StillProcessingDialogProps) {
  return (
    <Drawer
      title={`${action} still in progress`}
      onClose={onClose}
      footer={
        <>
          <button className="btn-s" onClick={onClose}>Close</button>
          <button className="btn-p" onClick={onContinueMonitoring}>Continue monitoring</button>
        </>
      }
    >
      <div style={{ fontSize: 13, lineHeight: 1.5 }}>{action} {REASON_COPY[reason]}</div>
      {context && Object.keys(context).length > 0 && (
        <div style={{ marginTop: 12 }}>
          {Object.entries(context).map(([k, v]) => (
            <div className="kv" key={k}><span className="kvk">{k}</span><span className="kvv">{v}</span></div>
          ))}
        </div>
      )}
    </Drawer>
  );
}
