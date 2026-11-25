/**
 * The files open in the editor survive a refresh.
 *
 * Remembered per browser tab (sessionStorage) with the workspace they belong to. After a refresh the tabs come back only for the SAME
 * workspace and only for files that still exist in it; an unsaved new file (Untitled-N, a local buffer) cannot come back, and unsaved
 * edits are not kept (the browser already warns before a reload that would drop them).
 */
import { readPersisted, writePersisted } from "../../shared/usePersistedState";
import type { UUID } from "../../types";
import { isLocalBufferId } from "./localBuffers";

export const EDITOR_TABS_KEY = "ws.editorTabs";

export interface StoredEditorTabs { workspaceId: UUID; openTabs: UUID[]; activeTab: UUID | null }

const isStoredTabs = (v: unknown): v is StoredEditorTabs | null => {
  if (v === null) return true;
  const o = v as Partial<StoredEditorTabs>;
  return !!o && typeof o === "object" && typeof o.workspaceId === "string" && Array.isArray(o.openTabs)
    && o.openTabs.every((t) => typeof t === "string") && (o.activeTab === null || typeof o.activeTab === "string");
};

export function readEditorTabs(): StoredEditorTabs | null {
  return readPersisted<StoredEditorTabs | null>(EDITOR_TABS_KEY, null, isStoredTabs);
}

/** Remember the open tabs of `workspaceId` (no tabs left = nothing remembered). Unsaved new files are left out. */
export function saveEditorTabs(workspaceId: UUID, openTabs: readonly UUID[], activeTab: UUID | null): void {
  const files = openTabs.filter((id) => !isLocalBufferId(id));
  const active = activeTab && files.includes(activeTab) ? activeTab : files[files.length - 1] ?? null;
  writePersisted<StoredEditorTabs | null>(EDITOR_TABS_KEY, files.length ? { workspaceId, openTabs: files, activeTab: active } : null, null);
}

/** What to reopen: the remembered tabs of THIS workspace that are still files of it, with the remembered active one (else the last). */
export function tabsToRestore(
  stored: StoredEditorTabs | null, workspaceId: UUID, existing: ReadonlySet<UUID>,
): { openTabs: UUID[]; activeTab: UUID | null } | null {
  if (!stored || stored.workspaceId !== workspaceId) return null;
  const openTabs = stored.openTabs.filter((id) => existing.has(id));
  if (!openTabs.length) return null;
  return { openTabs, activeTab: stored.activeTab && openTabs.includes(stored.activeTab) ? stored.activeTab : openTabs[openTabs.length - 1] };
}
