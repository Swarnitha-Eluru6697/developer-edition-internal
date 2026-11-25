/** The full-height left sidebar: product mark and nav sections (same look as the other InAiEra apps). */
import type { CSSProperties } from "react";
import { useDispatch, useSelector } from "react-redux";
import { usePermissions } from "../rbac/hooks";
import { PralaiLockup } from "../shared/ui/PralaiLogo";
import { setNavSection } from "../store/uiSlice";
import type { AppDispatch, RootState } from "../store";
import { useGetAlertCountsQuery } from "../features/alerts/api";
import { canOpenSection, visibleNavSections } from "./navigation";
import { Glyph } from "../shared/ui/Glyph";

const navRow = (active: boolean): CSSProperties => ({
  display: "flex", alignItems: "center", gap: 11, padding: "9px 12px", marginBottom: 2,
  fontSize: 13.5, fontWeight: active ? 600 : 500, borderRadius: 9, cursor: "pointer", position: "relative",
  color: active ? "var(--ws-nav-active-fg, #086347)" : "var(--ws-muted, #5B6D62)",
  background: active ? "var(--ws-nav-active-bg, #E3F6EA)" : "transparent",
  boxShadow: active ? "inset var(--ws-nav-stroke-w, 2px) 0 0 var(--ws-nav-stroke, #0A7D5A)" : "none",
  transition: "background .12s, color .12s",
});

export function Sidebar() {
  const dispatch = useDispatch<AppDispatch>();
  const navSection = useSelector((s: RootState) => s.ui.navSection);
  const permissions = usePermissions();
  // How many alerts are open (sent and not yet acknowledged): a badge on Alerts, like the Agents app's.
  const { data: alertCounts } = useGetAlertCountsQuery(undefined, { skip: !canOpenSection("alerts", permissions), pollingInterval: 60000 });

  return (
    <div style={{ width: 248, flexShrink: 0, background: "var(--ws-surface, #fff)", borderRight: "1px solid var(--ws-line, #E3E8E2)", display: "flex", flexDirection: "column", overflow: "clip" }}>
      <div style={{ height: 56, flexShrink: 0, display: "flex", alignItems: "center", gap: 10, padding: "0 18px", borderBottom: "1px solid var(--ws-line, #E3E8E2)" }}>
        <PralaiLockup height={34} label="Workspace" />
      </div>
      <div style={{ flex: 1, overflow: "auto", padding: "14px 12px 40px" }}>
        {visibleNavSections(permissions).map((section) => (
          <div key={section.label} style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 10.5, fontWeight: 600, color: "var(--ws-subtle, #73827A)", textTransform: "uppercase", letterSpacing: ".14em", padding: "8px 12px 6px" }}>{section.label}</div>
            {section.items.map(({ id, icon, label }) => {
              const active = navSection === id;
              return (
                <div
                  key={id}
                  onClick={() => dispatch(setNavSection(id))}
                  style={navRow(active)}
                  onMouseEnter={(e) => { if (!active) { e.currentTarget.style.background = "var(--ws-hover, #EEF2ED)"; e.currentTarget.style.color = "var(--ws-text, #14201B)"; } }}
                  onMouseLeave={(e) => { if (!active) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--ws-muted, #5B6D62)"; } }}
                >
                  <Glyph name={icon} size={17} style={{ color: active ? "var(--ws-nav-active-icon, #0A7D5A)" : "inherit", opacity: active ? 1 : 0.85 }} />
                  {label}
                  {id === "alerts" && !!alertCounts?.open && (
                    <span aria-label={`${alertCounts.open} open alerts`} style={{ marginLeft: "auto", background: "var(--ws-accent-bg, #CFE8DD)", color: "var(--ws-accent, #086347)", padding: "1px 7px", borderRadius: 999, fontSize: 11, fontWeight: 700 }}>
                      {alertCounts.open}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
