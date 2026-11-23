/** Left file panel of the editor: workspace switcher, Add New, file tree / Git changes, object explorer. */
import { useState } from "react";
import { ObjectExplorer } from "../../catalog";
import type { WorkspaceEditor } from "../hooks/useWorkspaceEditor";
import { AddNewMenu } from "./AddNewMenu";
import { FilePanelTabs } from "./FilePanelTabs";
import { WorkspaceSwitcher } from "./WorkspaceSwitcher";

export function WorkspaceFilePanel({ editor }: { editor: WorkspaceEditor }) {
  const { activeWs, activeTabId, appendToDraft } = editor;
  // Workspace ⋯ → Add file opens the Add New menu below it.
  const [addNewSignal, setAddNewSignal] = useState(0);
  return (
    <div style={{ borderRight: "0.5px solid #E5E7EB", display: "flex", flexDirection: "column", background: "#F9FAFB", overflow: "hidden", position: "relative" }}>
      <div style={{ padding: "10px 10px 8px", borderBottom: "0.5px solid #E5E7EB", position: "relative" }}>
        <WorkspaceSwitcher
          workspaces={editor.workspaces}
          activeWs={activeWs}
          onSelect={editor.setActiveWs}
          onNewWorkspace={editor.newWorkspace}
          onAddFile={() => setAddNewSignal((n) => n + 1)}
          onRenameWorkspace={editor.renameActiveWorkspace}
          onDeleteWorkspace={editor.deleteActiveWorkspace}
        />

        {/* File search lives in the header (app/HeaderSearch.tsx). */}
        <AddNewMenu onNewFile={editor.newFile} onNewDbtProject={editor.newDbtProject} openSignal={addNewSignal} />
      </div>

      {/* Files | Changes tabs + tree */}
      {activeWs && <FilePanelTabs workspaceId={activeWs} activeFileId={activeTabId} />}

      <ObjectExplorer onInsertTable={(t: string) => {
        if (activeTabId) appendToDraft(activeTabId, `\n-- Table: ${t}\n`);
      }} />
    </div>
  );
}
