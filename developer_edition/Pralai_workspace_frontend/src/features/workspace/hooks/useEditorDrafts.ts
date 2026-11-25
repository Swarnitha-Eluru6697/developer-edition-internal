import { useCallback, useEffect, useMemo, useState } from "react";
import { parseCells, toIpynb } from "../../notebook/notebookFormat";
import type { FileWithContent, UUID } from "../../../types";

/** What the editor holds for a file. A notebook is re-serialised, because that is what its editor writes
 *  back on every edit: comparing it with the file as stored would show an untouched notebook as changed. */
export const editorContent = (language: string, content: string): string =>
  language === "ipynb" ? toIpynb(parseCells(content)) : content;

/**
 * Editor text per open file id, and what it is compared against to know a file has UNSAVED changes.
 *
 * `drafts` is what is in the editor; `baselines` is what was last saved (the server's content when the
 * file first loaded, or what the last successful save wrote). A file is dirty when they differ. Nothing
 * is written to the server until the user saves, and a refetch never clobbers what they typed.
 */
export function useEditorDrafts(file: FileWithContent | undefined, activeTabId: UUID | null) {
  const [drafts, setDrafts] = useState<Record<UUID, string>>({});
  const [baselines, setBaselines] = useState<Record<UUID, string>>({});

  useEffect(() => {
    // `file` can briefly be the previous tab's data while the new one loads —
    // only seed a draft from the file that actually belongs to the active tab.
    if (!file || !activeTabId || file.id !== activeTabId) return;
    const content = editorContent(file.language, file.content);
    setDrafts((d) => (d[activeTabId] !== undefined ? d : { ...d, [activeTabId]: content }));
    setBaselines((b) => (b[activeTabId] !== undefined ? b : { ...b, [activeTabId]: content }));
  }, [file, activeTabId]);

  const setDraft = useCallback((fileId: UUID, content: string) => {
    setDrafts((d) => ({ ...d, [fileId]: content }));
  }, []);

  const appendToDraft = useCallback((fileId: UUID, text: string) => {
    setDrafts((d) => ({ ...d, [fileId]: (d[fileId] ?? "") + text }));
  }, []);

  /** A new buffer: its text, and the text that counts as "unchanged". */
  const startBuffer = useCallback((fileId: UUID, content: string, baseline: string) => {
    setDrafts((d) => ({ ...d, [fileId]: content }));
    setBaselines((b) => ({ ...b, [fileId]: baseline }));
  }, []);

  /** The file was saved with `content`: that is the new "unchanged". */
  const markSaved = useCallback((fileId: UUID, content: string) => {
    setBaselines((b) => ({ ...b, [fileId]: content }));
  }, []);

  /** Throw away the editor text (tab closed with "Don't Save"): reopening reloads the saved file. */
  const forget = useCallback((fileId: UUID) => {
    setDrafts((d) => { const { [fileId]: _x, ...rest } = d; return rest; });
    setBaselines((b) => { const { [fileId]: _x, ...rest } = b; return rest; });
  }, []);

  /** Drop every draft (the workspace they belong to was deleted). */
  const reset = useCallback(() => { setDrafts({}); setBaselines({}); }, []);

  const dirtyIds = useMemo(
    () => new Set(Object.keys(drafts).filter((id) => baselines[id] !== undefined && drafts[id] !== baselines[id])),
    [drafts, baselines],
  );
  const isDirty = useCallback((fileId: UUID) => dirtyIds.has(fileId), [dirtyIds]);

  return { drafts, setDraft, appendToDraft, startBuffer, markSaved, forget, reset, dirtyIds, isDirty };
}

export type EditorDrafts = ReturnType<typeof useEditorDrafts>;
