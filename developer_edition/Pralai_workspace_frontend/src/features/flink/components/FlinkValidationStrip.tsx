/** Result of the last FlinkSQL syntax check, shown as a strip under the editor tabs. */
import type { FlinkValidateResponse } from "../../../types";
import { Glyph } from "../../../shared/ui/Glyph";

const dismissBtn = {
  background: "none", border: "none", cursor: "pointer", color: "inherit",
  padding: 2, display: "flex", alignItems: "center", borderRadius: 4,
} as const;

export function FlinkValidationStrip({ result, onDismiss }: { result: FlinkValidateResponse; onDismiss: () => void }) {
  return (
    <div style={{
      flexShrink: 0, display: "flex", alignItems: "flex-start", gap: 8, padding: "7px 12px",
      borderBottom: "0.5px solid var(--ws-line, #E3E8E2)", fontSize: 12,
      background: result.valid ? "var(--ws-ok-bg, #eaf3e1)" : "var(--ws-bad-bg, #fbe9e9)",
      color: result.valid ? "var(--ws-ok, #2f6b14)" : "var(--ws-bad, #b23b3b)",
    }}>
      <i className={`ti ${result.valid ? "ti-circle-check" : "ti-alert-circle"}`} style={{ fontSize: 14, marginTop: 1, flexShrink: 0 }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        {result.valid ? (
          <>
            Valid FlinkSQL
            {result.statement_count != null && ` — ${result.statement_count} statement(s)`}
            {!!result.detected_sources?.length && (
              <span style={{ color: "var(--ws-muted, #5b6d62)" }}> · sources: {result.detected_sources.join(", ")}</span>
            )}
            {!!result.detected_sinks?.length && (
              <span style={{ color: "var(--ws-muted, #5b6d62)" }}> · sinks: {result.detected_sinks.join(", ")}</span>
            )}
          </>
        ) : (
          <div style={{ fontFamily: "monospace", fontSize: 11.5, whiteSpace: "pre-wrap", maxHeight: 90, overflow: "auto" }}>
            {result.errors?.length ? result.errors.join("\n") : "Invalid FlinkSQL"}
          </div>
        )}
      </div>
      <button onClick={onDismiss} title="Dismiss" style={dismissBtn}>
        <Glyph name="pi-x" size={13} />
      </button>
    </div>
  );
}
