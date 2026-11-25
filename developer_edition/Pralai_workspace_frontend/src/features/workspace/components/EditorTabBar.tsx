/** Open-file tabs plus the editor toolbar (AI chat toggle, Run / Validate). */
import { useDispatch, useSelector } from "react-redux";
import { useState } from "react";
import { setActiveTab, toggleChat } from "../../../store/uiSlice";
import { fileIcon } from "../fileTree";
import { IconAction } from "../../../shared/ui/IconAction";
import type { AppDispatch, RootState } from "../../../store";
import type { FileLanguage, UUID } from "../../../types";

interface Props {
  activeTabId: UUID | null;
  /** Tab text (the file's name), hover text (its path), and the tabs with unsaved changes. */
  tabLabel: (id: UUID) => string;
  tabTitle: (id: UUID) => string | undefined;
  dirtyIds: Set<UUID>;
  /** Closes a tab, asking first when it has unsaved changes. */
  onCloseTab: (id: UUID) => void;
  activeLanguage: FileLanguage | string | undefined;
  validatingFlink: boolean;
  onRun: () => void;
  onValidateFlink: () => void;
}

export function EditorTabBar({ activeTabId, tabLabel, tabTitle, dirtyIds, onCloseTab, activeLanguage, validatingFlink, onRun, onValidateFlink }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const openTabs = useSelector((s: RootState) => s.ui.openTabs);
  const showChat = useSelector((s: RootState) => s.ui.showChat);

  return (
    <div style={{ display: "flex", alignItems: "stretch", background: "var(--ws-bg, #F8FAF9)", borderBottom: "0.5px solid var(--ws-line, #E3E8E2)", flexShrink: 0, height: 36, overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "stretch", flex: 1, minWidth: 0, overflowX: "auto" }}>
        {openTabs.map((tabId) => {
          const label = tabLabel(tabId);
          const dirty = dirtyIds.has(tabId);
          const isActive = tabId === activeTabId;
          // The icon follows the file's type (name), like the Files panel — not always SQL.
          const tabIcon = fileIcon(label, isActive ? activeLanguage : undefined);
          return (
            <div
              key={tabId}
              title={tabTitle(tabId) ?? label}
              onClick={() => dispatch(setActiveTab(tabId))}
              style={{ display: "flex", alignItems: "center", gap: 5, padding: "0 13px", height: "100%", fontSize: 12, color: isActive ? "var(--ws-text, #14201B)" : "var(--ws-subtle, #73827a)", cursor: "pointer", borderRight: "0.5px solid var(--ws-line, #E3E8E2)", borderBottom: isActive ? "2px solid #0A7D5A" : "2px solid transparent", background: isActive ? "var(--ws-surface, #fff)" : "transparent", fontWeight: isActive ? 500 : 400, whiteSpace: "nowrap", flexShrink: 0 }}
            >
              <i className={`ti ${tabIcon.icon}`} style={{ fontSize: 13, color: tabIcon.color }} />
              {label}
              <TabClose dirty={dirty} onClose={() => onCloseTab(tabId)} />
            </div>
          );
        })}
        {/* No "+" here: it silently made a .sql regardless of what you
            wanted. New files come from Add New, which asks for the kind. */}
      </div>
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 5, padding: "0 10px", flexShrink: 0 }}>
        {/* The AI assistant lives with the open script, not in the app header. */}
        <IconAction
          icon="ti-sparkles"
          label={showChat ? "Close AI assistant (⌘K)" : "AI assistant (⌘K)"}
          on={showChat}
          onClick={() => dispatch(toggleChat())}
        />
        {activeLanguage === "flinksql" ? (<>
          <div style={{ width: 0.5, height: 16, background: "var(--ws-line-strong-fill, #cfd8d1)" }} />
          {/* Streaming jobs aren't run from the editor — they're deployed
              from Git under FlinkSQL → Jobs. Here you syntax-check. */}
          <div
            title="Validate FlinkSQL (⌘↵)"
            onClick={onValidateFlink}
            style={{ height: 25, padding: "0 11px", fontSize: 11.5, border: "0.5px solid #22d3ee", borderRadius: 6, display: "flex", alignItems: "center", gap: 5, cursor: "pointer", background: "var(--ws-info-bg, #ecfeff)", color: "var(--ws-info, #0e7490)", opacity: validatingFlink ? 0.6 : 1 }}
          >
            <i className="ti ti-checkup-list" style={{ fontSize: 12 }} />
            {validatingFlink ? "Validating…" : "Validate"}
          </div>
        </>) : (activeLanguage === "sql" || activeLanguage === "py") && (<>
          <div style={{ width: 0.5, height: 16, background: "var(--ws-line-strong-fill, #cfd8d1)" }} />
          <div
            title="Run (⌘↵)"
            onClick={onRun}
            style={{ width: 30, height: 30, borderRadius: "50%", background: "var(--ws-primary, #0A7D5A)", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer" }}
          >
            <i className="ti ti-player-play" style={{ fontSize: 13, color: "var(--ws-on-primary, #fff)" }} />
          </div>
        </>)}
      </div>
    </div>
  );
}

/** × to close; a dot instead while the file has unsaved changes (the × returns on hover). */
function TabClose({ dirty, onClose }: { dirty: boolean; onClose: () => void }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      onClick={(e) => { e.stopPropagation(); onClose(); }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={dirty ? "Unsaved changes — close…" : "Close"}
      style={{ background: "none", border: "none", cursor: "pointer", color: dirty && !hover ? "var(--ws-accent, #0A7D5A)" : "var(--ws-subtle, #73827a)", fontSize: dirty && !hover ? 14 : 11, padding: "0 0 0 5px", lineHeight: 1, minWidth: 12 }}
    >{dirty && !hover ? "●" : "×"}</button>
  );
}
