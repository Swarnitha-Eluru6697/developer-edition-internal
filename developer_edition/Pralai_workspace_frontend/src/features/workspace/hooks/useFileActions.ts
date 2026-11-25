import { useCallback } from "react";
import { useDispatch } from "react-redux";
import {
  useCreateFileMutation,
  useCreateWorkspaceMutation,
  useDeleteFileMutation,
  useDeleteWorkspaceMutation,
  useUpdateFileMutation,
  useUpdateWorkspaceMutation,
} from "../api";
import { buildDbtScaffold, normalizeDbtProjectName } from "../../dbt";
import { apiErrorMessage } from "../../../api/errors";
import { askConfirm, askText } from "../../../shared/ui/dialogStore";
import { reassignExecutionOwner } from "../../sql";
import { closeAllTabs, closeTab, notify, openTab } from "../../../store/uiSlice";
import type { AppDispatch } from "../../../store";
import type { FileLanguage, FileWithContent, UUID, Workspace } from "../../../types";
import { isUntitledPath, seedContent, suggestedSaveName, type NewFileLanguage } from "../newFiles";
import { isLocalBufferId, localBufferSaveName, type LocalLanguage } from "../localBuffers";
import { editorContent } from "./useEditorDrafts";
import type { EditorDrafts } from "./useEditorDrafts";
import type { useLocalBuffers } from "./useLocalBuffers";
import { deleteWorkspaceConfirm } from "../workspaces";

interface Deps {
  activeWs: UUID | null;
  setActiveWs: (id: UUID | null) => void;
  /** A workspace was deleted: stop showing it and fall back to another. */
  dropWorkspace: (id: UUID) => void;
  drafts: EditorDrafts;
  localBuffers: ReturnType<typeof useLocalBuffers>;
}

/** Creating, saving and scaffolding workspace files (and new workspaces). */
export function useFileActions({ activeWs, setActiveWs, dropWorkspace, drafts, localBuffers }: Deps) {
  const { startBuffer, markSaved, forget } = drafts;
  const { create: createLocal, remove: removeLocal, clear: clearLocal } = localBuffers;
  const { reset: resetDrafts } = drafts;
  const dispatch = useDispatch<AppDispatch>();
  const [createFile] = useCreateFileMutation();
  const [updateFile] = useUpdateFileMutation();
  const [deleteFile] = useDeleteFileMutation();
  const [createWorkspace] = useCreateWorkspaceMutation();
  const [deleteWorkspaceCall] = useDeleteWorkspaceMutation();
  const [updateWorkspaceCall] = useUpdateWorkspaceMutation();

  // Open a new file in a tab (optionally seeded, e.g. by codegen). It starts as an unsaved "Untitled-N" in
  // the editor and is only created on the server when saved — for every kind, notebooks included.
  const newFile = useCallback(async (language: NewFileLanguage, seed?: string): Promise<UUID | undefined> => {
    if (!activeWs) {
      dispatch(notify({ kind: "info", message: "Select or create a workspace first." }));
      return undefined;
    }
    const content = editorContent(language, seed ?? seedContent(language));
    const buffer = createLocal(language);
    // The template alone (a notebook's empty cell, the FlinkSQL header) isn't a change worth a prompt;
    // text the caller put there (codegen) is.
    startBuffer(buffer.id, content, seed === undefined ? content : "");
    dispatch(openTab(buffer.id));
    return buffer.id;
  }, [activeWs, createLocal, dispatch, startBuffer]);

  // Scaffold an EMPTY dbt project folder structure in the current workspace.
  // Each folder has a "+ file" action for the user's own models afterwards.
  const newDbtProject = useCallback(async () => {
    if (!activeWs) return;
    const raw = await askText({ title: "New dbt project", label: "Project folder name", hint: "Lowercase letters, digits and underscores.", defaultValue: "my_project", confirmLabel: "Create", icon: "ti-brand-databricks" });
    if (raw === null) return;
    const dbtName = normalizeDbtProjectName(raw);
    let created = 0;
    let firstFileId: UUID | null = null;
    for (const f of buildDbtScaffold(dbtName)) {
      try {
        const row = await createFile({ workspaceId: activeWs, body: f }).unwrap();
        if (f.path.endsWith("dbt_project.yml")) firstFileId = row.id;
        created++;
      } catch (e) {
        dispatch(notify({ kind: "error", message: `Failed on ${f.path}: ${apiErrorMessage(e, "create failed", { prefer: "detail" })}` }));
        break;
      }
    }
    if (firstFileId) dispatch(openTab(firstFileId));
    if (created > 0) dispatch(notify({ kind: "success", message: `Created dbt project "${dbtName}". Add .sql / .yml files in each folder (hover a folder → + file), then push to Git and register it.` }));
  }, [activeWs, createFile, dispatch]);

  // Save a file (a notebook's cells included). Resolves true when it was saved, false when the user cancelled or it
  // failed — callers (closing a tab) keep the tab open on false.
  //   - an unsaved "Untitled-N" is "saved as": ask for a name, create the file, switch the tab to it
  //   - a legacy server scratch file (untitled<ms>.*) is saved as: create the named one, delete the scratch
  //   - anything else is updated in place
  const save = useCallback(async (file: FileWithContent | undefined, content: string): Promise<boolean> => {
    if (!activeWs || !file) return false;
    try {
      const local = isLocalBufferId(file.id);
      if (local || isUntitledPath(file.path)) {
        const suggested = local ? localBufferSaveName(file.language as LocalLanguage) : suggestedSaveName(file.path);
        const asked = await askText({ title: file.language === "ipynb" ? "Save notebook" : "Save file", label: file.language === "ipynb" ? "Notebook name" : "File name", defaultValue: suggested, confirmLabel: "Save", icon: "ti-device-floppy" });
        if (!asked || !asked.trim()) return false;
        // A notebook is always saved as .ipynb, whatever was typed.
        const name = file.language === "ipynb" && !/\.ipynb$/i.test(asked.trim()) ? `${asked.trim()}.ipynb` : asked;
        const created = await createFile({
          workspaceId: activeWs,
          // FileMetadata's `language` isn't an enum in the OpenAPI doc the way
          // FileCreate's is; the backend only ever writes one of that set.
          body: { path: name.trim(), language: file.language as FileLanguage, content },
        }).unwrap();
        startBuffer(created.id, content, content);
        dispatch(reassignExecutionOwner({ from: file.id, to: created.id }));   // keep the results the file had
        dispatch(openTab(created.id));
        dispatch(closeTab(file.id));
        forget(file.id);
        if (local) removeLocal(file.id);
        else { try { await deleteFile({ workspaceId: activeWs, fileId: file.id }).unwrap(); } catch { /* leave scratch if delete fails */ } }
        dispatch(notify({ kind: "success", message: `Saved as ${name.trim()}` }));
      } else {
        // `if_match_sha256` (sourced from `sha256`) is the optimistic-
        // concurrency field the backend reads, so a stale write is refused.
        await updateFile({ workspaceId: activeWs, fileId: file.id, body: { content, if_match_sha256: file.sha256 } }).unwrap();
        markSaved(file.id, content);
        dispatch(notify({ kind: "success", message: `Saved ${file.path}` }));
      }
      return true;
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Save failed") }));
      return false;
    }
  }, [activeWs, createFile, deleteFile, dispatch, forget, markSaved, removeLocal, startBuffer, updateFile]);

  const newWorkspace = useCallback(async () => {
    const name = await askText({ title: "New workspace", label: "Workspace name", placeholder: "e.g. analytics", confirmLabel: "Create", icon: "ti-layout-board-split" });
    if (!name?.trim()) return;
    const ws = await createWorkspace({ name: name.trim() }).unwrap();
    setActiveWs(ws.id);
  }, [createWorkspace, setActiveWs]);

  // Delete a workspace after the user types its name. Only the workspace and its stored files go; the Git
  // repository behind it is never touched. Whatever was open belonged to it, so the editor is cleared and
  // the next workspace (if any) is shown.
  const deleteWorkspace = useCallback(async (workspace: Pick<Workspace, "id" | "name"> | undefined) => {
    if (!workspace) return;
    if (!(await askConfirm(deleteWorkspaceConfirm(workspace.name)))) return;
    try {
      await deleteWorkspaceCall(workspace.id).unwrap();
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Could not delete the workspace.", { prefer: "detail" }) }));
      return;
    }
    dispatch(closeAllTabs());
    resetDrafts();
    clearLocal();
    dropWorkspace(workspace.id);  // hide it now; the next workspace (if any) is shown
    dispatch(notify({ kind: "success", message: `Deleted workspace "${workspace.name}".` }));
  }, [clearLocal, deleteWorkspaceCall, dispatch, dropWorkspace, resetDrafts]);

  // Rename a workspace (its files and Git binding are unaffected).
  const renameWorkspace = useCallback(async (workspace: Pick<Workspace, "id" | "name"> | undefined) => {
    if (!workspace) return;
    const name = (await askText({ title: "Rename workspace", label: "Workspace name", defaultValue: workspace.name, confirmLabel: "Save", icon: "pi-pencil" }))?.trim();
    if (!name || name === workspace.name) return;
    try {
      await updateWorkspaceCall({ id: workspace.id, body: { name } }).unwrap();
      dispatch(notify({ kind: "success", message: `Renamed workspace to "${name}".` }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Could not rename the workspace.", { prefer: "detail" }) }));
    }
  }, [dispatch, updateWorkspaceCall]);

  return { newFile, newDbtProject, save, newWorkspace, renameWorkspace, deleteWorkspace };
}
