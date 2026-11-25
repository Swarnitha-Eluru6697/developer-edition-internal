/**
 * Render tab: the stack-specific artifact compiled from the selected binding's
 * ruleset — a dbt schema.yml block, a FlinkSQL quality job, or a GX suite +
 * Soda checks. Cached server-side against a hash of the ruleset.
 */
import { useGetDqRenderQuery } from "../api";
import { apiErrorMessage } from "../../../api/errors";
import type { DqBinding } from "../../../types";
import { cellEllipsis, codeBlock, jobTypeLabel } from "../dqUi";
import { renderArtifacts } from "../dqUtils";

export function DqRenderView({ binding, onBack }: { binding: DqBinding; onBack: () => void }) {
  const { data: render, isFetching: loadingRender, error: renderError } = useGetDqRenderQuery(binding.id);
  const artifacts = renderArtifacts(render);
  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <button
          onClick={onBack}
          title="Back to bindings"
          style={{ background: "none", border: "0.5px solid var(--ws-line-strong, #cfd8d1)", borderRadius: 6, cursor: "pointer", color: "var(--ws-muted, #5b6d62)", height: 26, width: 26, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}
        ><i className="ti ti-arrow-left" style={{ fontSize: 14 }} /></button>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 600, ...cellEllipsis }}>
            {binding.ruleset?.name ?? "Ruleset"} → <span style={{ fontFamily: "monospace" }}>{binding.target_table}</span>
          </div>
          <div style={{ fontSize: 11, color: "var(--ws-subtle, #73827a)", fontFamily: "monospace", ...cellEllipsis }}>
            {binding.ucs_fqn}
          </div>
        </div>
      </div>

      {loadingRender && (
        <div style={{ padding: 24, textAlign: "center", color: "var(--ws-subtle, #73827a)", fontSize: 13 }}>Rendering…</div>
      )}

      {!loadingRender && renderError != null && (
        <div style={{ padding: "10px 12px", background: "var(--ws-bad-bg, #fbe9e9)", border: "0.5px solid var(--ws-bad-line, #fecaca)", borderRadius: 6, fontSize: 12.5, color: "var(--ws-bad, #b23b3b)", lineHeight: 1.5 }}>
          {apiErrorMessage(renderError, "Could not render this binding.", { prefer: "detail" })}
        </div>
      )}

      {!loadingRender && render && artifacts.length === 0 && (
        <div style={{ padding: 24, textAlign: "center", color: "var(--ws-muted, #5b6d62)", fontSize: 12.5, lineHeight: 1.55 }}>
          The ruleset produced no artifact for a {jobTypeLabel(render.job_type)}.
          <br />Rules with no column, or no equivalent on this engine, are skipped.
        </div>
      )}

      {!loadingRender && artifacts.map((a) => (
        <div key={a.label} style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ws-text-2, #2E3D36)", marginBottom: 3 }}>{a.label}</div>
          <div style={{ fontSize: 11, color: "var(--ws-subtle, #73827a)", marginBottom: 6, lineHeight: 1.45 }}>{a.hint}</div>
          <pre style={codeBlock}>{a.body}</pre>
        </div>
      ))}

      {!loadingRender && render && (
        <div style={{ fontSize: 11, color: "var(--ws-subtle, #73827a)", borderTop: "0.5px solid var(--ws-line, #E3E8E2)", paddingTop: 8 }}>
          Ruleset hash <span style={{ fontFamily: "monospace" }}>{render.content_hash.slice(0, 16)}…</span>
          {" · "}
          Rendered {render.rendered_at ? new Date(render.rendered_at).toLocaleString() : "—"}
        </div>
      )}
    </div>
  );
}
