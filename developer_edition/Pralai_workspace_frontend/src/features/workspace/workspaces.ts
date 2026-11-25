import type { Workspace } from "../../types";

/** Case-insensitive name filter for the workspace switcher. */
export function filterWorkspaces(workspaces: Workspace[] | undefined, search: string): Workspace[] {
  return workspaces?.filter((w) => w.name.toLowerCase().includes(search.toLowerCase())) ?? [];
}

/** Which workspace the editor should be showing.
 *
 *  - `current` while it is in the list;
 *  - `current` also while it has never been seen in the list — a workspace you have just created is
 *    selected before the list refetches, and must not be mistaken for a deleted one;
 *  - otherwise (it WAS in the list and is gone, i.e. deleted) the first workspace, or none;
 *  - while the list has not loaded, `current` is kept.
 *  `seen` holds every workspace id the list has ever contained. */
export function resolveActiveWorkspace(
  workspaces: Pick<Workspace, "id">[] | undefined,
  current: string | null,
  seen: ReadonlySet<string> = new Set(),
): string | null {
  if (!workspaces) return current;
  if (current && workspaces.some((w) => w.id === current)) return current;
  if (current && !seen.has(current)) return current;
  return workspaces[0]?.id ?? null;
}

/** The workspace to open when the app loads: the one the user last worked in, if it is still in the list; otherwise the first.
 *  (Without this every refresh opened whichever workspace the list happened to return first, so the editor jumped to a different
 *  workspace each time.) */
export function pickInitialWorkspace(workspaces: Pick<Workspace, "id">[], remembered: string | null): string | null {
  if (remembered && workspaces.some((w) => w.id === remembered)) return remembered;
  return workspaces[0]?.id ?? null;
}

const ACTIVE_WS_KEY = "inaiera.activeWorkspace";

/** Where the last active workspace is remembered: per user (a shared browser must not carry one person's choice to another). */
export function activeWorkspaceStorageKey(userId: string | undefined | null): string | null {
  return userId ? `${ACTIVE_WS_KEY}.${userId}` : null;
}

export function readRememberedWorkspace(userId: string | undefined | null): string | null {
  const key = activeWorkspaceStorageKey(userId);
  if (!key) return null;
  try { return window.localStorage.getItem(key); } catch { return null; }
}

export function rememberWorkspace(userId: string | undefined | null, workspaceId: string | null): void {
  const key = activeWorkspaceStorageKey(userId);
  if (!key) return;
  try {
    if (workspaceId) window.localStorage.setItem(key, workspaceId);
    else window.localStorage.removeItem(key);
  } catch { /* storage unavailable (private mode, quota): the choice just is not remembered */ }
}

/** The confirmation shown before deleting a workspace. The name must be typed, like deleting a database
 *  connection, and the text says plainly what is — and is not — deleted. */
export function deleteWorkspaceConfirm(name: string) {
  return {
    title: "Delete workspace",
    message:
      `Delete the workspace "${name}"? Every file stored in this workspace is removed, and any change you ` +
      "have not committed and pushed is lost. Your Git repository is NOT deleted: nothing is removed from " +
      "GitHub (or any other Git host), and the repo stays exactly as it is. This cannot be undone.",
    confirmLabel: "Delete workspace",
    confirmText: name,
    danger: true,
  };
}

/** What the workspace switcher's label should show. Until the list has loaded there is nothing true to
 *  say, so it is a placeholder, not a made-up name ("My Workspace" used to flash on every refresh). */
export type WorkspaceLabel = { kind: "loading" } | { kind: "name"; text: string } | { kind: "none" };

export function workspaceLabel(workspaces: Pick<Workspace, "id" | "name">[] | undefined, activeWs: string | null): WorkspaceLabel {
  if (!workspaces) return { kind: "loading" };
  const active = workspaces.find((w) => w.id === activeWs);
  if (active) return { kind: "name", text: active.name };
  // The list is here but the active one is not chosen yet (it is picked a moment later), or there are none.
  return workspaces.length > 0 && !activeWs ? { kind: "loading" } : { kind: "none" };
}
