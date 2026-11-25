/** Runs tab: the project's run history; selecting a run expands its node events. */
import type { DbtRun, DbtRunEvent, UUID } from "../../../types";
import { timeAgo } from "../../../shared/format";
import { EMPTY_PAD, HEAD_STYLE, ROW_PAD, RUN_COLS, STATUS_BG, STATUS_COLOR, gridRow } from "../styles";
import { nodeEventSummary } from "../runEvents";
import { failedModelsText, type RunUnit } from "../runCounts";

interface Props {
  runs: DbtRun[] | undefined;
  events: DbtRunEvent[] | undefined;
  selectedRunId: UUID | null;
  onSelectRun: (id: UUID | null) => void;
  /** What a run counts: dbt models, or a SQL job's statements. */
  unit?: RunUnit;
}

export function DbtRunsTab({ runs, events, selectedRunId, onSelectRun, unit = "model" }: Props) {
  return (
    <div>
      {/* The project name lives in the screen header, so this is just the run list. */}
      {!runs?.length && (
        <div style={{ padding: EMPTY_PAD, textAlign: "center", color: "var(--ws-subtle, #73827a)", fontSize: 13 }}>
          No runs yet — use <strong style={{ color: "var(--ws-muted, #5b6d62)" }}>Run now</strong> to build this project.
        </div>
      )}
      {!!runs?.length && (
        <div style={{ ...HEAD_STYLE, ...gridRow(RUN_COLS) }}>
          <div>Status</div><div>Trigger</div><div>Models</div><div>When</div><div>Duration</div><div />
        </div>
      )}
      {runs?.map((run) => <RunRow key={run.id} run={run} unit={unit} selected={run.id === selectedRunId} onSelect={() => onSelectRun(run.id === selectedRunId ? null : run.id)} />)}
      {selectedRunId && events && (
        <div style={{ padding: ROW_PAD, background: "var(--ws-bg, #F8FAF9)", borderTop: "0.5px solid var(--ws-line, #E3E8E2)" }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>Run events</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            {events.map((e) => {
              const { ok, seconds, message, output } = nodeEventSummary(e);
              return (
                <div key={e.id} style={{ fontSize: 11.5, fontFamily: "monospace", color: "var(--ws-text-2, #4b5a53)" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                    {e.event_type === "node_finish" ? (
                      <i className={`ti ${ok ? "ti-check" : "ti-x"}`} style={{ color: ok ? "var(--ws-ok, #2f6b14)" : "var(--ws-bad, #b23b3b)", fontSize: 12 }} />
                    ) : <i className="ti ti-info-circle" style={{ fontSize: 12, color: "var(--ws-subtle, #73827a)" }} />}
                    <span style={{ flex: 1 }}>{e.node_id ?? e.event_type}</span>
                    {seconds != null && <span style={{ color: "var(--ws-subtle, #73827a)" }}>{seconds.toFixed(2)}s</span>}
                  </div>
                  {message && <div style={{ marginLeft: 19, color: ok ? "var(--ws-muted, #5b6d62)" : "var(--ws-bad, #b23b3b)", fontFamily: "inherit" }}>{message}</div>}
                  {output && (
                    <details style={{ marginLeft: 19 }} open={!ok}>
                      <summary style={{ cursor: "pointer", color: "var(--ws-muted, #5b6d62)" }}>Output</summary>
                      <pre style={{ margin: "4px 0 0", padding: 8, background: "var(--ws-surface, #fff)", border: "0.5px solid var(--ws-line, #E3E8E2)", borderRadius: 4,
                        maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{output}</pre>
                    </details>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function RunRow({ run, unit, selected, onSelect }: { run: DbtRun; unit: RunUnit; selected: boolean; onSelect: () => void }) {
  const duration = run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : "";
  const ago = run.created_at ? timeAgo(run.created_at) : "";
  return (
    <div
      onClick={onSelect}
      style={{ ...gridRow(RUN_COLS), padding: ROW_PAD, cursor: "pointer", borderBottom: "0.5px solid var(--ws-line, #eef2ed)", background: selected ? "var(--ws-info-bg, #f0f7ff)" : "var(--ws-surface, white)", fontSize: 12.5 }}
      onMouseEnter={(e) => !selected && (e.currentTarget.style.background = "var(--ws-hover, #F8FAF9)")}
      onMouseLeave={(e) => !selected && (e.currentTarget.style.background = "var(--ws-surface, white)")}
    >
      {/* Hugs its text. Stretching it to fill an equal-width track would make a
          200px-wide "failed" badge on a wide window. */}
      <span>
        <span style={{
          display: "inline-block", background: STATUS_BG[run.status] ?? "var(--ws-fill, #eef2ed)",
          color: STATUS_COLOR[run.status] ?? "var(--ws-subtle, #73827a)", padding: "2px 8px", borderRadius: 4,
          fontSize: 11.5, fontWeight: 600, textAlign: "center", minWidth: 72,
        }}>{run.status}</span>
      </span>
      <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        <span style={{ color: "var(--ws-muted, #5b6d62)" }}>{run.trigger}</span>
        {run.select && <span style={{ marginLeft: 6, fontFamily: "monospace", fontSize: 11, color: "var(--ws-subtle, #73827a)" }}>–s {run.select}</span>}
      </span>
      {/* An absent value still renders its cell — dropping it shifted every
          later column left and broke the alignment for that one row. */}
      <span style={{ fontSize: 11.5, color: "var(--ws-subtle, #73827a)" }}>
        {run.models_total != null
          ? failedModelsText(run, unit) ?? `${run.models_succeeded ?? 0}/${run.models_total} ${unit}s`
          : "—"}
      </span>
      <span style={{ fontSize: 11.5, color: "var(--ws-subtle, #73827a)" }}>{ago || "—"}</span>
      <span style={{ fontSize: 11.5, color: "var(--ws-subtle, #73827a)" }}>{duration || "—"}</span>
      <i className="ti ti-chevron-right" style={{ color: "var(--ws-faint, #cfd8d1)", fontSize: 13 }} />
    </div>
  );
}
