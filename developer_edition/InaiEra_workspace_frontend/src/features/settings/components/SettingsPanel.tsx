/**
 * Settings (Govern → Settings): which engines and services this Workspace is
 * connected to, checked live, and — for anything not fully set up — which
 * settings are still required and what stops working without them. The codegen
 * agent card also lets a holder of workspace:settings:manage (the backend reports it as `can_edit`) choose which model answers the
 * ⌘K assistant.
 */
import { useGetEnginesQuery } from "../api";
import { summarizeEngines } from "../engineSummary";
import { apiErrorStatus } from "../../../api/errors";
import type { EngineStatus } from "../../../types";
import { STATE } from "../engineState";
import { CodegenAgentCard } from "./CodegenAgentCard";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";

const GROUPS: { category: EngineStatus["category"]; label: string }[] = [
  { category: "engine", label: "Engines" },
  { category: "runner", label: "Runners" },
];  // The "platform" services (UCS, database, Redis, object storage, secrets) are not shown here, and not counted in the summary line.

export function SettingsPanel() {
  const { data, isFetching, error, refetch } = useGetEnginesQuery(undefined, { refetchOnMountOrArgChange: true });
  const engines = (data?.engines ?? []).filter((e) => GROUPS.some((g) => g.category === e.category));
  const { connected, needsSetup } = summarizeEngines(engines);

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", background: "#F9FAFB" }}>
      <div style={{ padding: "22px 28px 6px", display: "flex", alignItems: "flex-end", gap: 12 }}>
        <div>
          <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".12em", color: "#C2410C", textTransform: "uppercase" }}>Govern</div>
          <h1 style={{ fontFamily: "var(--disp)", fontSize: 23, fontWeight: 600, letterSpacing: "-.01em", color: "#1F2937", margin: "7px 0 0" }}>Settings</h1>
          <div style={{ fontSize: 13.5, color: "#6b7280", marginTop: 4 }}>
            {data
              ? <>{connected} of {engines.length} services connected{needsSetup > 0 && <> · <span style={{ color: "#b45309" }}>{needsSetup} need attention</span></>}</>
              : "Engines and services this workspace is connected to."}
          </div>
        </div>
        <IconAction
          icon={ACTION_ICON.refresh}
          label={isFetching ? "Checking services…" : "Re-check services"}
          disabled={isFetching}
          style={{ marginLeft: "auto" }}
          onClick={() => refetch()}
        />
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "10px 28px 28px" }}>
        {error && (
          <div style={{ padding: "10px 12px", background: "#fef2f2", border: "1px solid #fecaca", borderRadius: 8, color: "#b91c1c", fontSize: 13 }}>
            Couldn't load engine status — {apiErrorStatus(error) ?? "request failed"}.
          </div>
        )}
        {!data && !error && <div style={{ fontSize: 13, color: "#9ca3af", padding: "12px 0" }}>Checking services…</div>}

        {GROUPS.map(({ category, label }) => {
          const items = engines.filter((e) => e.category === category);
          if (!items.length) return null;
          return (
            <section key={category} style={{ marginTop: 18 }}>
              <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: ".12em", color: "#8a93a8", textTransform: "uppercase", marginBottom: 10 }}>{label}</div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16 }}>
                {items.map((e) => (
                  e.key === "codegen" ? (
                    <CodegenAgentCard key={e.key} engine={e} onRefetchEngines={refetch} />
                  ) : (
                    <EngineCard key={e.key} engine={e} />
                  )
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}

function EngineCard({ engine: e }: { engine: EngineStatus }) {
  const st = STATE[e.status];
  const needsDetails = e.missing.length > 0;
  return (
    <div style={{ background: "#fff", border: "1px solid #e8eaee", borderRadius: 12, padding: "18px 20px", boxShadow: "0 1px 2px rgba(16,24,40,.04)", display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: "#111827", flex: 1, minWidth: 0 }}>{e.name}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 500, color: st.text, whiteSpace: "nowrap" }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%", background: st.dot }} />
          {st.label}
        </div>
      </div>
      <div style={{ fontSize: 12.5, color: "#6b7280", lineHeight: 1.5 }}>{e.description}</div>

      {(e.detail || e.latency_ms != null || e.endpoint) && (
        <div style={{ fontSize: 12, color: "#4b5563", display: "flex", flexDirection: "column", gap: 3 }}>
          {e.latency_ms != null && e.status === "connected" && <span>Responded in {e.latency_ms} ms</span>}
          {e.detail && <span style={{ color: e.status === "unreachable" ? "#b91c1c" : "#4b5563", wordBreak: "break-word" }}>{e.detail}</span>}
          {e.endpoint && <code style={{ fontSize: 11.5, color: "#6b7280", wordBreak: "break-all" }}>{e.endpoint}</code>}
        </div>
      )}

      {needsDetails && (
        <div style={{ marginTop: 4, background: "#fffbeb", border: "1px solid #fde68a", borderRadius: 8, padding: "10px 12px" }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "#92400e", marginBottom: 4 }}>Details required</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: e.impact ? 6 : 0 }}>
            {e.missing.map((m) => (
              <code key={m} style={{ fontSize: 11.5, background: "#fff", border: "1px solid #fde68a", borderRadius: 5, padding: "1px 6px", color: "#92400e" }}>{m}</code>
            ))}
          </div>
          {e.impact && <div style={{ fontSize: 12, color: "#92400e", lineHeight: 1.5 }}>Until set: {e.impact}</div>}
        </div>
      )}
      {!needsDetails && e.status === "unreachable" && e.impact && (
        <div style={{ fontSize: 12, color: "#b91c1c", lineHeight: 1.5 }}>Impact: {e.impact}</div>
      )}
    </div>
  );
}
