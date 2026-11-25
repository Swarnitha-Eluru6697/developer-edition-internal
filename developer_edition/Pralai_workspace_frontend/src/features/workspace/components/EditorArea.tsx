/**
 * The Workspace editor's main column: tab bar, FlinkSQL check strip, the
 * editor (Monaco, or NotebookView for .ipynb), the Results/History dock and
 * the status bar.
 */
import { useSelector } from "react-redux";
import { FlinkValidationStrip } from "../../flink";
import { NotebookView } from "../../notebook";
import { ResultsDock } from "../../sql";
import type { RootState } from "../../../store";
import type { FileLanguage } from "../../../types";
import type { WorkspaceEditor } from "../hooks/useWorkspaceEditor";
import { CodeEditor } from "./CodeEditor";
import { EditorStatusBar } from "./EditorStatusBar";
import { EditorTabBar } from "./EditorTabBar";

export function EditorArea({ editor }: { editor: WorkspaceEditor }) {
  const { activeWs, activeTabId, file, draft, drafts, setDraft } = editor;
  const resultsOpen = useSelector((s: RootState) => s.ui.resultsOpen);
  const historyOpen = useSelector((s: RootState) => s.ui.historyOpen);
  const language = file?.language;

  return (
    <>
      <EditorTabBar
        activeTabId={activeTabId}
        tabLabel={editor.tabLabel}
        tabTitle={editor.tabTitle}
        dirtyIds={editor.dirtyIds}
        onCloseTab={editor.requestCloseTab}
        activeLanguage={language}
        validatingFlink={editor.validatingFlink}
        onRun={editor.run}
        onValidateFlink={editor.validateFlink}
      />

      {language === "flinksql" && editor.flinkCheck && (
        <FlinkValidationStrip result={editor.flinkCheck} onDismiss={editor.dismissFlinkCheck} />
      )}

      <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
        {file && language === "ipynb" && activeWs && activeTabId ? (
          <NotebookView
            key={activeTabId}
            fileId={activeTabId}
            sessionId={editor.execSessionId}
            initialContent={drafts[activeTabId] ?? file.content ?? ""}
            onChange={(content) => setDraft(activeTabId, content)}
            onSave={editor.save}
          />
        ) : (
          <>
            <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
              {file && activeTabId && (
                <CodeEditor
                  language={file.language as FileLanguage}
                  value={draft}
                  onChange={(v) => setDraft(activeTabId, v)}
                  onRun={editor.run}
                  onSave={editor.save}
                  onSelectionChange={editor.setSelectedSql}
                />
              )}
            </div>
            {/* FlinkSQL files never produce query results, so the dock stays hidden. */}
            {language !== "flinksql" && (resultsOpen || historyOpen) && (
              <ResultsDock
                workspaceId={activeWs}
                filePath={file?.path ?? null}
                height={editor.resultsHeight}
                onHeightChange={editor.setResultsHeight}
              />
            )}
          </>
        )}
      </div>

      <EditorStatusBar workspaceId={activeWs} />
    </>
  );
}
