/**
 * HomeScreen — shown when no file is open.
 * Matches the Snowflake pattern: SVG illustration + "Create a file to
 * get started" + quick-create buttons + keyboard shortcuts table with
 * General / Notebooks tabs.
 */
import { useState } from "react";
import { useDispatch } from "react-redux";
import { openGitModal } from "../../../store/uiSlice";
import type { AppDispatch } from "../../../store";
import { usePermissions } from "../../../rbac/hooks";
import { allowedNewFileOptions, type NewFileLanguage } from "../newFiles";

interface Props {
  onNewFile: (language: NewFileLanguage) => void;
  onNewDbtProject: () => void;
}

const SHORTCUTS_GENERAL = [
  { task: "Run selected", keys: ["⌘", "Return"] },
  { task: "Run all", keys: ["⌘", "⇧", "Return"] },
  { task: "Format SQL", keys: ["⌘", "⇧", "O"] },
  { task: "Find in file", keys: ["⌘", "F"] },
  { task: "Split pane horizontally", keys: ["CTRL", "\\"] },
  { task: "Open codegen chat", keys: ["⌘", "K"] },
  { task: "Save file", keys: ["⌘", "S"] },
  { task: "Close focused tab", keys: ["CTRL", "Q"] },
];

const SHORTCUTS_NOTEBOOKS = [
  { task: "Run cell", keys: ["⌘", "Return"] },
  { task: "Run all cells", keys: ["⌘", "⇧", "Return"] },
  { task: "Insert cell below", keys: ["B"] },
  { task: "Delete cell", keys: ["D", "D"] },
  { task: "Toggle markdown/code", keys: ["M"] },
];

export function HomeScreen({ onNewFile, onNewDbtProject }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const [shortcutTab, setShortcutTab] = useState<"general" | "notebooks">("general");
  const shortcuts = shortcutTab === "general" ? SHORTCUTS_GENERAL : SHORTCUTS_NOTEBOOKS;
  const permissions = usePermissions();
  // The same rule as Add New: creating anything needs workspace:files:edit, and each kind its own permission on top.
  const creatable = new Set(allowedNewFileOptions(permissions).map((o) => o.kind));
  const canCreate = creatable.size > 0;

  return (
    <div style={{
      flex: 1, display: "flex", flexDirection: "column", alignItems: "center",
      justifyContent: "center", background: "var(--ws-surface, #fff)", padding: "24px", overflow: "auto",
      textAlign: "center",
    }}>
      {/* Illustration */}
      <svg width="90" height="80" viewBox="0 0 90 80" fill="none" style={{ marginBottom: 18, opacity: 0.85 }}>
        <rect style={{ fill: "var(--ws-accent-bg, #E3F6EA)", stroke: "var(--ws-line-strong, #B7E0CB)" }} x="15" y="35" width="60" height="38" rx="5" strokeWidth="1.5" />
        <rect style={{ fill: "var(--ws-accent-bg, #CFE8DD)", stroke: "var(--ws-accent, #0A7D5A)" }} x="25" y="20" width="40" height="28" rx="5" strokeWidth="1.5" />
        <rect style={{ fill: "var(--ws-line-strong, #B7E0CB)", stroke: "var(--ws-accent, #0A7D5A)" }} x="35" y="8" width="20" height="20" rx="5" strokeWidth="1.5" />
        <circle style={{ fill: "var(--ws-accent, #0A7D5A)" }} cx="45" cy="18" r="4" />
        <line style={{ stroke: "var(--ws-accent, #0A7D5A)" }} x1="35" y1="28" x2="25" y2="35" strokeWidth="1.5" />
        <line style={{ stroke: "var(--ws-accent, #0A7D5A)" }} x1="55" y1="28" x2="65" y2="35" strokeWidth="1.5" />
        <line style={{ stroke: "var(--ws-accent, #0A7D5A)" }} x1="45" y1="28" x2="45" y2="35" strokeWidth="1.5" />
        <rect style={{ fill: "var(--ws-accent-bg, #E3F6EA)", stroke: "var(--ws-line-strong, #B7E0CB)" }} x="22" y="48" width="14" height="16" rx="2" strokeWidth="1" />
        <rect style={{ fill: "var(--ws-accent-bg, #E3F6EA)", stroke: "var(--ws-line-strong, #B7E0CB)" }} x="40" y="45" width="10" height="19" rx="2" strokeWidth="1" />
        <rect style={{ fill: "var(--ws-accent-bg, #E3F6EA)", stroke: "var(--ws-line-strong, #B7E0CB)" }} x="54" y="50" width="14" height="14" rx="2" strokeWidth="1" />
      </svg>

      <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>{canCreate ? "Create a file to get started" : "Open a file to get started"}</div>
      <div style={{ fontSize: 13.5, color: "var(--ws-muted, #5b6d62)", marginBottom: 24 }}>
        Author SQL, dbt models, and streaming jobs — all in one workspace
      </div>

      <div style={{ display: "flex", gap: 10, marginBottom: 30, flexWrap: "wrap", justifyContent: "center" }}>
        {[
          { label: "SQL file", icon: "ti-file-type-sql", color: "var(--ws-accent, #0A7D5A)", action: () => onNewFile("sql"), show: creatable.has("sql") },
          { label: "Notebook", icon: "ti-notebook", color: "var(--ws-accent, #0A7D5A)", action: () => onNewFile("ipynb"), show: creatable.has("ipynb") },
          { label: "FlinkSQL editor", icon: "ti-wave-sine", color: "var(--ws-accent, #0A7D5A)", action: () => onNewFile("flinksql"), show: creatable.has("flinksql") },
          { label: "dbt Project", icon: "ti-brand-databricks", color: "var(--ws-accent, #0A7D5A)", action: () => onNewDbtProject(), show: creatable.has("dbt") },
          { label: "From Git repository", icon: "ti-git-fork", color: "var(--ws-accent, #0A7D5A)", action: () => dispatch(openGitModal()), show: canCreate },
        ].filter((o) => o.show).map(({ label, icon, color, action }) => (
          <button
            key={label}
            onClick={action}
            style={{
              display: "flex", alignItems: "center", gap: 7, height: 36, padding: "0 16px",
              fontSize: 13, border: "0.5px solid var(--ws-line-strong, #cfd8d1)", borderRadius: 7, background: "var(--ws-surface, #fff)",
              cursor: "pointer", color: "var(--ws-text-2, #2E3D36)",
            }}
            onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--ws-line-strong, #B7E0CB)"; e.currentTarget.style.color = "var(--ws-accent, #086347)"; }}
            onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--ws-line-strong, #cfd8d1)"; e.currentTarget.style.color = "var(--ws-text-2, #2E3D36)"; }}
          >
            <i className={`ti ${icon}`} style={{ fontSize: 16, color }} />
            {label}
          </button>
        ))}
      </div>

      {/* Keyboard shortcuts */}
      <div style={{ width: "100%", maxWidth: 560, textAlign: "left" }}>
        <div style={{ fontSize: 11, color: "var(--ws-subtle, #73827a)", textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 8 }}>
          Keyboard shortcuts
        </div>
        <div style={{ display: "flex", borderBottom: "0.5px solid var(--ws-line, #e3e8e2)", marginBottom: 12, gap: 0 }}>
          {(["general", "notebooks"] as const).map((t) => (
            <div
              key={t}
              onClick={() => setShortcutTab(t)}
              style={{
                padding: "6px 16px", fontSize: 13, cursor: "pointer", textTransform: "capitalize",
                color: shortcutTab === t ? "var(--ws-text, #14201B)" : "var(--ws-muted, #5b6d62)",
                borderBottom: shortcutTab === t ? "2px solid #0A7D5A" : "2px solid transparent",
                fontWeight: shortcutTab === t ? 500 : 400,
              }}
            >
              {t}
            </div>
          ))}
        </div>
        <div style={{ display: "flex", flexDirection: "column" }}>
          {shortcuts.map(({ task, keys }) => (
            <div key={task} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "0.5px solid var(--ws-line, #eef2ed)", fontSize: 13 }}>
              <span style={{ color: "var(--ws-muted, #5b6d62)" }}>{task}</span>
              <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                {keys.map((k, i) => (
                  <span key={i}>
                    <span style={{ background: "var(--ws-fill, #eef2ed)", border: "0.5px solid var(--ws-line-strong, #cfd8d1)", borderRadius: 4, padding: "2px 7px", fontFamily: "monospace", fontSize: 11.5, color: "var(--ws-muted, #5b6d62)", fontWeight: 500 }}>{k}</span>
                    {i < keys.length - 1 && <span style={{ color: "var(--ws-subtle, #73827a)", margin: "0 2px", fontSize: 11 }}>+</span>}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
