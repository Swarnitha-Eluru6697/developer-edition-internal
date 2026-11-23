/**
 * App.tsx — Workspace SPA shell: a thin composition of the feature modules.
 *
 * Layout (matches the InAiEra/Snowflake pattern):
 *   ┌──────────────┬───────────────────┬──────────────────────┬──────────────┐
 *   │  Sidebar     │  File Panel       │  Main Content        │  AI Chat     │
 *   │  240px       │  225px            │  flex:1              │  285px (opt) │
 *   └──────────────┴───────────────────┴──────────────────────┴──────────────┘
 *   Sidebar sections: BUILD → Workspace (the editor, as above); JOBS → dbt,
 *   Flink and GOVERN → Data Quality, Settings are full-page — no file panel,
 *   no chat.
 *
 * Where things live:
 *   app/navigation.ts            sidebar sections + permission gating
 *   rbac/                        permissions from GET /api/v1/me (usePermissions / useHasPermission)
 *   app/hooks/useShellEffects.ts 401 redirect, nav guard, chat clamp, ⌘S/⌘↵/⌘K
 *   features/workspace           editor controller (useWorkspaceEditor), file
 *                                panel, tabs, editor area, home screen
 *   features/{sql,notebook,flink,dbt,dq,git,codegen,catalog,settings}
 *                                each feature's screens, hooks and API slice
 */
import { useState } from "react";
import { useSelector } from "react-redux";
// The icon font (every `ti ti-*` icon) ships WITH the app. It used to be linked from cdn.jsdelivr.net, which a locked-down server or
// portal can block, leaving blank squares where the icons should be.
import "@tabler/icons-webfont/dist/tabler-icons.min.css";
import { useGetMeQuery } from "../api/client";
import { DatabaseConnectionModal } from "../features/catalog";
import { CodegenChatColumn } from "../features/codegen";
import { AlertsPanel } from "../features/alerts";
import { DataQualityPanel } from "../features/dq";
import { DbtRunnerPanel } from "../features/dbt";
import { FlinkPanel } from "../features/flink";
import { GitModal } from "../features/git";
import { SettingsPanel } from "../features/settings";
import { EditorArea, HomeScreen, WorkspaceFilePanel, useWorkspaceEditor } from "../features/workspace";
import { DialogHost } from "../shared/ui/DialogHost";
import { usePermissionsState } from "../rbac/hooks";
import { ACCESS_PERMISSION } from "../rbac/permissions";
import type { RootState } from "../store";
import { AppHeader } from "./AppHeader";
import { AuthErrorScreen } from "./AuthErrorScreen";
import { NoAccessScreen } from "./NoAccessScreen";
import { Sidebar } from "./Sidebar";
import { Toast } from "./Toast";
import {
  useChatWidthClamp,
  useKeyboardShortcuts,
  useGitOAuthReturn,
  useNavGuard,
  usePersistNavSection,
  useUnauthorizedRedirect,
} from "./hooks/useShellEffects";

export function App() {
  const { data: me, error: meError } = useGetMeQuery();
  const { heldPermissions, status: permissionsStatus, error: permissionsError } = usePermissionsState();
  // workspace:access:view = may open Workspace at all. Until it is known, nothing that calls the Workspace API is started. Checked on
  // the HELD set: "view as role" narrows what is shown, it never locks the user out of the app (they land on Workspace instead).
  const canAccess = heldPermissions.includes(ACCESS_PERMISSION);
  const ui = useSelector((s: RootState) => s.ui);
  const editor = useWorkspaceEditor(canAccess ? me : undefined);
  // The shell animates grid-template-columns; that easing fights a live drag
  // of the chat divider, so it's suppressed while resizing.
  const [chatResizing, setChatResizing] = useState(false);

  useUnauthorizedRedirect(meError);
  useGitOAuthReturn();
  useNavGuard();
  usePersistNavSection();
  useChatWidthClamp();
  useKeyboardShortcuts({
    editorShortcuts: editor.file?.language !== "ipynb",
    onSave: editor.save,
    onRun: editor.run,
  });

  if (!me) {
    if (!meError) return <div style={{ padding: 24, fontFamily: "sans-serif" }}>Authenticating…</div>;
    return <AuthErrorScreen error={meError} />;
  }
  if (permissionsStatus === "loading") return <div style={{ padding: 24, fontFamily: "sans-serif" }}>Authenticating…</div>;
  if (!canAccess) return <NoAccessScreen error={permissionsStatus === "error" ? permissionsError : undefined} />;

  const showHome = ui.openTabs.length === 0 || !editor.activeTabId;
  // Jobs (dbt / Flink), DQ and Settings are standalone pages: no file panel, no chat.
  const isEditorView = ui.navSection === "ws";
  // Just the content region (file panel | main | chat) — the dark sidebar is a
  // separate full-height column, so the white header spans only the area to
  // its right.
  const gridCols = !isEditorView ? "minmax(0, 1fr)" : ui.showChat ? `225px 1fr ${ui.chatWidth}px` : "225px 1fr";

  const mainContent = (() => {
    switch (ui.navSection) {
      case "dbt": return <DbtRunnerPanel key="dbt" />;
      case "sqljobs": return <DbtRunnerPanel key="sql" kind="sql" />;
      case "pythonjobs": return <DbtRunnerPanel key="python" kind="python" />;
      case "flink": return <FlinkPanel />;
      case "dq": return <DataQualityPanel />;
      case "alerts": return <AlertsPanel />;
      case "settings": return <SettingsPanel />;
      default: return showHome
        ? <HomeScreen onNewFile={editor.newFile} onNewDbtProject={editor.newDbtProject} />
        : <EditorArea editor={editor} />;
    }
  })();

  return (
    <div style={{ display: "flex", height: "100vh", width: "100%", overflow: "clip" }}>
      <Sidebar />

      {/* Right side: white header bar + content grid */}
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "clip" }}>
        <AppHeader workspaces={editor.workspaces} activeWs={editor.activeWs} onSelectWorkspace={editor.setActiveWs} />
        <Toast />

        <div style={{ display: "grid", gridTemplateColumns: gridCols, gridTemplateRows: "minmax(0, 1fr)", flex: 1, minHeight: 0, overflow: "clip", transition: chatResizing ? "none" : "grid-template-columns 0.15s" }}>
          {isEditorView && <WorkspaceFilePanel editor={editor} />}

          <div style={{ display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, overflow: "clip", background: "#fff" }}>
            {mainContent}
          </div>

          {isEditorView && ui.showChat && editor.activeWs && (
            <CodegenChatColumn
              workspaceId={editor.activeWs}
              activeFileId={editor.activeTabId}
              activeFileContent={editor.draft}
              activeFileLanguage={editor.file?.language ?? null}
              execSessionId={editor.execSessionId}
              onInsertCode={editor.insertGeneratedCode}
              onResizingChange={setChatResizing}
            />
          )}

          {/* Modals */}
          {ui.gitModalOpen && <GitModal />}
          {ui.connectionModalOpen && <DatabaseConnectionModal />}
          <DialogHost />
        </div>
      </div>
    </div>
  );
}
