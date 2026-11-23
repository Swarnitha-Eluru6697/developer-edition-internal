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
      justifyContent: "center", background: "#fff", padding: "24px", overflow: "auto",
      textAlign: "center",
    }}>
      {/* Illustration */}
      <svg width="90" height="80" viewBox="0 0 90 80" fill="none" style={{ marginBottom: 18, opacity: 0.85 }}>
        <rect x="15" y="35" width="60" height="38" rx="5" fill="#FFF7ED" stroke="#FED7AA" strokeWidth="1.5" />
        <rect x="25" y="20" width="40" height="28" rx="5" fill="#FFEDD5" stroke="#F97316" strokeWidth="1.5" />
        <rect x="35" y="8" width="20" height="20" rx="5" fill="#FED7AA" stroke="#F97316" strokeWidth="1.5" />
        <circle cx="45" cy="18" r="4" fill="#F97316" />
        <line x1="35" y1="28" x2="25" y2="35" stroke="#F97316" strokeWidth="1.5" />
        <line x1="55" y1="28" x2="65" y2="35" stroke="#F97316" strokeWidth="1.5" />
        <line x1="45" y1="28" x2="45" y2="35" stroke="#F97316" strokeWidth="1.5" />
        <rect x="22" y="48" width="14" height="16" rx="2" fill="#FFF7ED" stroke="#FED7AA" strokeWidth="1" />
        <rect x="40" y="45" width="10" height="19" rx="2" fill="#FFF7ED" stroke="#FED7AA" strokeWidth="1" />
        <rect x="54" y="50" width="14" height="14" rx="2" fill="#FFF7ED" stroke="#FED7AA" strokeWidth="1" />
      </svg>

      <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>{canCreate ? "Create a file to get started" : "Open a file to get started"}</div>
      <div style={{ fontSize: 13.5, color: "#6b7280", marginBottom: 24 }}>
        Author SQL, dbt models, and streaming jobs — all in one workspace
      </div>

      <div style={{ display: "flex", gap: 10, marginBottom: 30, flexWrap: "wrap", justifyContent: "center" }}>
        {[
          { label: "SQL file", icon: "ti-file-type-sql", color: "#F97316", action: () => onNewFile("sql"), show: creatable.has("sql") },
          { label: "Notebook", icon: "ti-notebook", color: "#F97316", action: () => onNewFile("ipynb"), show: creatable.has("ipynb") },
          { label: "FlinkSQL editor", icon: "ti-wave-sine", color: "#F97316", action: () => onNewFile("flinksql"), show: creatable.has("flinksql") },
          { label: "dbt Project", icon: "ti-brand-databricks", color: "#F97316", action: () => onNewDbtProject(), show: creatable.has("dbt") },
          { label: "From Git repository", icon: "ti-git-fork", color: "#F97316", action: () => dispatch(openGitModal()), show: canCreate },
        ].filter((o) => o.show).map(({ label, icon, color, action }) => (
          <button
            key={label}
            onClick={action}
            style={{
              display: "flex", alignItems: "center", gap: 7, height: 36, padding: "0 16px",
              fontSize: 13, border: "0.5px solid #d1d5db", borderRadius: 7, background: "#fff",
              cursor: "pointer", color: "#374151",
            }}
            onMouseEnter={(e) => { e.currentTarget.style.borderColor = "#FED7AA"; e.currentTarget.style.color = "#C2410C"; }}
            onMouseLeave={(e) => { e.currentTarget.style.borderColor = "#d1d5db"; e.currentTarget.style.color = "#374151"; }}
          >
            <i className={`ti ${icon}`} style={{ fontSize: 16, color }} />
            {label}
          </button>
        ))}
      </div>

      {/* Keyboard shortcuts */}
      <div style={{ width: "100%", maxWidth: 560, textAlign: "left" }}>
        <div style={{ fontSize: 11, color: "#9ca3af", textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 8 }}>
          Keyboard shortcuts
        </div>
        <div style={{ display: "flex", borderBottom: "0.5px solid #e5e7eb", marginBottom: 12, gap: 0 }}>
          {(["general", "notebooks"] as const).map((t) => (
            <div
              key={t}
              onClick={() => setShortcutTab(t)}
              style={{
                padding: "6px 16px", fontSize: 13, cursor: "pointer", textTransform: "capitalize",
                color: shortcutTab === t ? "#1F2937" : "#6b7280",
                borderBottom: shortcutTab === t ? "2px solid #F97316" : "2px solid transparent",
                fontWeight: shortcutTab === t ? 500 : 400,
              }}
            >
              {t}
            </div>
          ))}
        </div>
        <div style={{ display: "flex", flexDirection: "column" }}>
          {shortcuts.map(({ task, keys }) => (
            <div key={task} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "0.5px solid #f3f4f6", fontSize: 13 }}>
              <span style={{ color: "#6b7280" }}>{task}</span>
              <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                {keys.map((k, i) => (
                  <span key={i}>
                    <span style={{ background: "#f3f4f6", border: "0.5px solid #d1d5db", borderRadius: 4, padding: "2px 7px", fontFamily: "monospace", fontSize: 11.5, color: "#6b7280", fontWeight: 500 }}>{k}</span>
                    {i < keys.length - 1 && <span style={{ color: "#9ca3af", margin: "0 2px", fontSize: 11 }}>+</span>}
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
