/**
 * FilePanelTabs — Files | Changes tabs for the left sidebar.
 * Files tab: recursive file tree from /files/tree endpoint.
 * Changes tab: the git feature's GitChangesPanel (commit, push, PR).
 */
import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useCreateFileMutation, useDeleteFileMutation, useGetFileTreeQuery } from "../api";
import { useHasPermission } from "../../../rbac/hooks";
import { GitChangesPanel, dirtyFileCount, useGetGitBindingQuery, useGetGitStatusQuery } from "../../git";
import { apiErrorMessage, apiErrorStatus } from "../../../api/errors";
import { FILE_PANEL_TAB_KEY, setFilePanelTab, openTab, closeTab, openGitModal, notify, saveTreeOpen, setTreeNodeOpen, treeNodeKey } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { UUID, FileTreeNode } from "../../../types";
import { askText, askConfirm } from "../../../shared/ui/dialogStore";
import { collectFileIds, emptyNotebook, fileIcon, fileTreeView, findNodeByPath, languageFromFileName } from "../fileTree";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";
import { writePersisted } from "../../../shared/usePersistedState";

interface Props {
  workspaceId: UUID;
  activeFileId: UUID | null;
}

export function FilePanelTabs({ workspaceId, activeFileId }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const filePanelTab = useSelector((s: RootState) => s.ui.filePanelTab);
  const treeOpen = useSelector((s: RootState) => s.ui.treeOpen);
  // Remember which folders are open across a refresh (per browser tab).
  useEffect(() => saveTreeOpen(treeOpen), [treeOpen]);
  // ...and which tab (Files / Changes) is open.
  useEffect(() => writePersisted(FILE_PANEL_TAB_KEY, filePanelTab, "files"), [filePanelTab]);
  const { data: binding } = useGetGitBindingQuery(workspaceId);
  // Load the tree on the workspace's current branch (imported repos may be on
  // "master" etc.), falling back to the default "main".
  const branch = binding?.current_branch ?? undefined;
  const { data: tree, isLoading: treeLoading, refetch: refetchTree } = useGetFileTreeQuery({ workspaceId, branch });
  const { data: gitStatus } = useGetGitStatusQuery(workspaceId);
  const [deleteFile] = useDeleteFileMutation();
  const [createFile] = useCreateFileMutation();
  const canEditFiles = useHasPermission("workspace:files:edit");

  // Create a file inside a folder (dirPath ""= workspace root). User types the
  // name incl. extension; language is inferred so it opens with the right editor.
  const handleCreateFile = async (dirPath: string) => {
    const name = await askText({ title: "New file", label: `File name in ${dirPath || "root"}`, hint: "Include the extension, e.g. stg_customers.sql or schema.yml.", confirmLabel: "Create", icon: "ti-file-plus" });
    if (!name || !name.trim()) return;
    const clean = name.trim().replace(/^\/+/, "");
    const path = dirPath ? `${dirPath}/${clean}` : clean;
    const language = languageFromFileName(clean);
    const content = language === "ipynb" ? emptyNotebook(false) : "";
    try {
      const row = await createFile({ workspaceId, body: { path, language, content } }).unwrap();
      dispatch(openTab(row.id));
      refetchTree();
    } catch (e) {
      // If it already exists (e.g. dbt_project.yml from the scaffold), open it
      // instead of erroring — the user almost always meant to edit it.
      const dup = apiErrorStatus(e) === 409 || /already exists/i.test(apiErrorMessage(e, "", { prefer: "detail" }));
      if (dup) {
        const existing = findNodeByPath(tree, path);
        if (existing?.id) {
          dispatch(openTab(existing.id));
          dispatch(notify({ kind: "info", message: `"${clean}" already exists — opened it for editing.` }));
        } else {
          dispatch(notify({ kind: "info", message: `"${clean}" already exists in ${dirPath || "root"}.` }));
        }
        return;
      }
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Create failed", { prefer: "detail" }) }));
    }
  };

  // Create a (virtual) folder by dropping a .gitkeep placeholder inside it.
  const handleCreateFolder = async (dirPath: string) => {
    const name = await askText({ title: "New folder", label: `Folder name in ${dirPath || "root"}`, confirmLabel: "Create", icon: "ti-folder-plus" });
    if (!name || !name.trim()) return;
    const clean = name.trim().replace(/^\/+|\/+$/g, "");
    const path = `${dirPath ? dirPath + "/" : ""}${clean}/.gitkeep`;
    try {
      await createFile({ workspaceId, body: { path, language: "other", content: "" } }).unwrap();
      refetchTree();
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Create failed", { prefer: "detail" }) }));
    }
  };

  const handleDeleteFile = async (fileId: UUID, name: string) => {
    if (!(await askConfirm({ title: "Delete file", message: `Delete "${name}"? This cannot be undone.`, confirmLabel: "Delete", danger: true }))) return;
    try {
      await deleteFile({ workspaceId, fileId }).unwrap();
      dispatch(closeTab(fileId));
      refetchTree();
    } catch {
      /* no-op; tree stays as-is on failure */
    }
  };

  // Delete a folder = delete every file under it (there are no real dir rows to
  // delete; a folder exists only because files live under its path).
  const handleDeleteFolder = async (node: FileTreeNode) => {
    const ids = collectFileIds(node);
    if (ids.length === 0) return;
    if (!(await askConfirm({ title: "Delete folder", message: `Delete folder "${node.name}" and its ${ids.length} file(s)? This cannot be undone.`, confirmLabel: "Delete", danger: true }))) return;
    let failed = 0;
    for (const id of ids) {
      try { await deleteFile({ workspaceId, fileId: id }).unwrap(); dispatch(closeTab(id)); }
      catch { failed++; }
    }
    refetchTree();
    if (failed) dispatch(notify({ kind: "error", message: `Deleted with ${failed} error(s).` }));
    else dispatch(notify({ kind: "success", message: `Deleted folder "${node.name}".` }));
  };
  // Modified + untracked. (This read `modified.length ?? 0 + untracked.length`
  // before — operator precedence meant untracked files were never counted.)
  const dirtyCount = dirtyFileCount(gitStatus);

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minHeight: 0 }}>
      {/* Tab bar */}
      <div style={{ display: "flex", borderBottom: "0.5px solid #E5E7EB", flexShrink: 0 }}>
        {(["files", "changes"] as const).map((t) => (
          <div
            key={t}
            onClick={() => dispatch(setFilePanelTab(t))}
            style={{
              flex: 1, textAlign: "center", padding: "6px 0", fontSize: 12, cursor: "pointer",
              color: filePanelTab === t ? "#1F2937" : "#888",
              borderBottom: filePanelTab === t ? "2px solid #F97316" : "2px solid transparent",
              fontWeight: filePanelTab === t ? 500 : 400,
              display: "flex", alignItems: "center", justifyContent: "center", gap: 5,
            }}
          >
            {t === "files" ? "Files" : <>Changes {dirtyCount > 0 && <span style={{ background: "#fef3c7", color: "#b45309", padding: "1px 5px", borderRadius: 4, fontSize: 10.5, fontWeight: 600 }}>{dirtyCount}</span>}</>}
          </div>
        ))}
      </div>

      {/* Files tab */}
      {filePanelTab === "files" && (
        <div style={{ flex: 1, overflow: "auto", padding: 6, display: "flex", flexDirection: "column" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 4, padding: "2px 4px 6px", flexShrink: 0 }}>
            <span style={{ fontSize: 10.5, color: "#9ca3af", textTransform: "uppercase", letterSpacing: ".06em", flex: 1 }}>Explorer</span>
            {canEditFiles && (<>
              <button title="New file at root" onClick={() => handleCreateFile("")} style={hdrBtn}><i className="ti ti-file-plus" style={{ fontSize: 14 }} /></button>
              <button title="New folder at root" onClick={() => handleCreateFolder("")} style={hdrBtn}><i className="ti ti-folder-plus" style={{ fontSize: 14 }} /></button>
            </>)}
          </div>
          {(() => {
            const view = fileTreeView(tree, treeLoading);
            if (view === "tree" && tree) {
              return <TreeNode node={tree} depth={0} workspaceId={workspaceId} activeFileId={activeFileId} onSelect={(id) => dispatch(openTab(id))} onDelete={handleDeleteFile} onDeleteFolder={handleDeleteFolder} onCreateFile={handleCreateFile} onCreateFolder={handleCreateFolder} />;
            }
            if (view === "loading") {
              // Quiet placeholder rows while the tree loads — not the "empty workspace" prompt.
              return (
                <div aria-label="Loading files" style={{ display: "flex", flexDirection: "column", gap: 9, padding: "8px 8px" }}>
                  {[70, 55, 80, 62, 48].map((w, i) => <div key={i} style={{ height: 9, width: `${w}%`, borderRadius: 4, background: "#F3F4F6" }} />)}
                </div>
              );
            }
            return (
              <div style={{ padding: "12px 8px", fontSize: 12, color: "#888", textAlign: "center" }}>
                Empty workspace.{" "}
                <button onClick={() => dispatch(openGitModal())} style={{ background: "none", border: "none", color: "#C2410C", cursor: "pointer", fontSize: 12, textDecoration: "underline" }}>
                  Connect a Git repo
                </button>
              </div>
            );
          })()}
        </div>
      )}

      {/* Changes tab */}
      {filePanelTab === "changes" && <GitChangesPanel workspaceId={workspaceId} />}
    </div>
  );
}

function TreeNode({
  node, depth, workspaceId, activeFileId, onSelect, onDelete, onDeleteFolder, onCreateFile, onCreateFolder,
}: { node: FileTreeNode; depth: number; workspaceId: UUID; activeFileId: UUID | null; onSelect: (id: UUID) => void; onDelete: (id: UUID, name: string) => void; onDeleteFolder: (node: FileTreeNode) => void; onCreateFile: (dirPath: string) => void; onCreateFolder: (dirPath: string) => void }) {
  // Open/closed lives in the store (see uiSlice.treeOpen) so it survives this tree being
  // unmounted when you switch to the Changes tab or to another page.
  const dispatch = useDispatch<AppDispatch>();
  const key = treeNodeKey(workspaceId, node.path);
  const open = useSelector((s: RootState) => s.ui.treeOpen[key]) ?? depth < 2;
  const setOpen = (v: boolean) => dispatch(setTreeNodeOpen({ key, open: v }));
  const [hover, setHover] = useState(false);
  const canEditFiles = useHasPermission("workspace:files:edit");
  if (node.type === "directory" && node.name === "") {
    return <>{(node.children ?? []).map((c) => <TreeNode key={c.path} node={c} depth={depth} workspaceId={workspaceId} activeFileId={activeFileId} onSelect={onSelect} onDelete={onDelete} onDeleteFolder={onDeleteFolder} onCreateFile={onCreateFile} onCreateFolder={onCreateFolder} />)}</>;
  }
  const isDir = node.type === "directory";
  const icon = isDir ? null : fileIcon(node.name, node.language);

  return (
    <div>
      <div
        onClick={() => isDir ? setOpen(!open) : (node.id ? onSelect(node.id) : undefined)}
        style={{
          display: "flex", alignItems: "center", gap: 6,
          padding: `3px ${8 + depth * 12}px 3px ${6 + depth * 12}px`,
          borderRadius: 5, cursor: "pointer", fontSize: 12,
          color: "#6B7280",
          background: "transparent",
        }}
        onMouseEnter={(e) => { setHover(true); e.currentTarget.style.background = "#F9FAFB"; }}
        onMouseLeave={(e) => { setHover(false); e.currentTarget.style.background = "transparent"; }}
      >
        {isDir ? (
          <>
            <i className={`ti ${open ? "ti-chevron-down" : "ti-chevron-right"}`} style={{ fontSize: 10, color: "#9ca3af" }} />
            <i className="ti ti-folder" style={{ fontSize: 13, color: "#F97316" }} />
          </>
        ) : (
          <>
            <span style={{ width: 14 }} />
            <i className={`ti ${icon?.icon}`} style={{ fontSize: 13, color: icon?.color }} />
          </>
        )}
        <span style={{ fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>{node.name}</span>
        {node.is_dirty && <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#f59e0b", flexShrink: 0 }} title="Unsaved changes" />}
        {isDir && hover && canEditFiles && (
          <>
            <IconAction size="sm" icon="ti-file-plus" label="New file in this folder"
              onClick={(e) => { e.stopPropagation(); setOpen(true); onCreateFile(node.path); }} />
            <IconAction size="sm" icon="ti-folder-plus" label="New folder in this folder"
              onClick={(e) => { e.stopPropagation(); setOpen(true); onCreateFolder(node.path); }} />
            <IconAction size="sm" icon={ACTION_ICON.delete} label="Delete folder (and everything in it)" danger
              onClick={(e) => { e.stopPropagation(); onDeleteFolder(node); }} />
          </>
        )}
        {!isDir && node.id && canEditFiles && (
          // Shown on row hover, like the folder actions; kept in layout so the row doesn't jump.
          <IconAction size="sm" icon={ACTION_ICON.delete} label="Delete file" danger
            style={{ visibility: hover ? "visible" : "hidden" }}
            onClick={(e) => { e.stopPropagation(); if (node.id) onDelete(node.id, node.name); }} />
        )}
      </div>
      {isDir && open && (node.children ?? []).map((c) => (
        <TreeNode key={c.path} node={c} depth={depth + 1} workspaceId={workspaceId} activeFileId={activeFileId} onSelect={onSelect} onDelete={onDelete} onDeleteFolder={onDeleteFolder} onCreateFile={onCreateFile} onCreateFolder={onCreateFolder} />
      ))}
    </div>
  );
}

const hdrBtn: CSSProperties = {
  background: "none", border: "none", cursor: "pointer", color: "#6b7280",
  padding: "2px 3px", display: "flex", alignItems: "center", borderRadius: 4,
};
