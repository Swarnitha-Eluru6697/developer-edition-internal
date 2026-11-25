import React from "react";
import StatePill, { RecordErrorsFlag, failingRecords } from "./StatePill";
import { NO_VALUE, formatMetricValue } from "../connectorMetrics";

export type DirFilter = "all" | "source" | "sink";

export interface RailRow {
  connector: string;
  name: string;
  direction: "source" | "sink";
  poolLabel: string;
  state: string;
  /** Kafka Connect has not answered yet: show a neutral placeholder instead of UNKNOWN. */
  loading?: boolean;
  /** records/s; null = no sample (rendered as a dash, never 0). */
  rate: number | null;
  /** records/s that failed in the last 5 minutes (see RecordErrorsFlag); null = no sample. */
  errorRate?: number | null;
}

/** The searchable connector list on the left of Monitoring: pick one to see its metrics. */
export default function MonitoringRail({ rows, selected, onSelect, filter, onFilter, dir, onDir }: {
  rows: RailRow[]; selected: string | null; onSelect: (connector: string) => void;
  filter: string; onFilter: (v: string) => void; dir: DirFilter; onDir: (d: DirFilter) => void;
}) {
  const q = filter.trim().toLowerCase();
  const list = rows.filter((r) => (dir === "all" || r.direction === dir) && (!q || `${r.name} ${r.connector}`.toLowerCase().includes(q)));
  return (
    <aside className="rail card" aria-label="Connectors">
      <div className="rail-h">
        <label htmlFor="railSearch" className="sr-only">Find a connector</label>
        <input id="railSearch" className="rail-search" type="search" placeholder="Find a connector" value={filter}
          onChange={(e) => onFilter(e.target.value)} autoComplete="off" />
        <div className="dc-tabs rail-f" role="tablist" aria-label="Connector type">
          {([["all", "All"], ["source", "Sources"], ["sink", "Sinks"]] as const).map(([v, l]) => (
            <button key={v} type="button" role="tab" aria-selected={dir === v} onClick={() => onDir(v)}>{l}</button>
          ))}
        </div>
      </div>
      <div className="rail-list" role="listbox" aria-label="Connectors">
        {list.length ? list.map((r) => (
          <button key={r.connector} type="button" role="option" aria-selected={r.connector === selected}
            className={`ri${r.connector === selected ? " on" : ""}`} data-connector={r.connector} onClick={() => onSelect(r.connector)}>
            <span className="ri-top">
              <span className="ri-name" title={r.name}>{r.name}</span>
              <span className="ri-state">
                {!r.loading && failingRecords(r.state, r.errorRate) && <RecordErrorsFlag errorRate={r.errorRate as number} />}
                <StatePill state={r.state} loading={r.loading} />
              </span>
            </span>
            <span className="ri-sub">
              <span>{r.direction} · {r.poolLabel} pool</span>
              <span className="num">{r.rate === null ? NO_VALUE : formatMetricValue(r.rate, "records/s")}</span>
            </span>
          </button>
        )) : <div className="empty">No connectors match</div>}
      </div>
    </aside>
  );
}
