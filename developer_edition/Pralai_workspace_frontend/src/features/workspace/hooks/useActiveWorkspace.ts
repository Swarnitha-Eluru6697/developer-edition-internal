import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useListWorkspacesQuery } from "../api";
import { clearPendingWorkspace } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { CurrentUser, UUID } from "../../../types";
import { pickInitialWorkspace, readRememberedWorkspace, rememberWorkspace, resolveActiveWorkspace } from "../workspaces";

/** The workspace the editor is showing: the first one by default, a freshly created one (selected
 *  straight away, or via the Git modal once the list includes it), and — when the active one is
 *  deleted — the next one. */
export function useActiveWorkspace(me: CurrentUser | undefined) {
  const dispatch = useDispatch<AppDispatch>();
  const pendingWorkspace = useSelector((s: RootState) => s.ui.pendingWorkspace);
  const { data: listed } = useListWorkspacesQuery(undefined, { skip: !me });
  const [activeWs, setActiveWs] = useState<UUID | null>(null);
  // Workspaces just deleted: hidden at once, before the list refetch drops them.
  const [deleted, setDeleted] = useState<UUID[]>([]);
  const workspaces = useMemo(() => listed?.filter((w) => !deleted.includes(w.id)), [listed, deleted]);
  // Every id the list has contained, to tell "just created, not listed yet" from "deleted".
  const seen = useRef(new Set<UUID>());

  // On the first load of the list, reopen the workspace the user last worked in (else the first); afterwards, move off one that
  // was deleted. The choice is remembered per user, so a refresh does not jump to another workspace.
  const initialised = useRef(false);
  useEffect(() => {
    if (!workspaces) return;
    workspaces.forEach((w) => seen.current.add(w.id));
    if (!initialised.current) {
      initialised.current = true;
      const first = activeWs ?? pickInitialWorkspace(workspaces, readRememberedWorkspace(me?.user_id));
      if (first !== activeWs) { setActiveWs(first); return; }
    }
    const next = resolveActiveWorkspace(workspaces, activeWs, seen.current);
    if (next !== activeWs) setActiveWs(next);
  }, [workspaces, activeWs, me?.user_id]);

  // Remember the choice (only once a workspace is actually selected: the initial null must not erase it).
  useEffect(() => {
    if (activeWs) rememberWorkspace(me?.user_id, activeWs);
  }, [activeWs, me?.user_id]);

  // Switch to a freshly-created workspace once the list refetch includes it.
  useEffect(() => {
    if (pendingWorkspace && workspaces?.some((w) => w.id === pendingWorkspace)) {
      setActiveWs(pendingWorkspace);
      dispatch(clearPendingWorkspace());
    }
  }, [pendingWorkspace, workspaces, dispatch]);

  /** The workspace was deleted: stop showing it and fall back to the next one. */
  const dropWorkspace = useCallback((id: UUID) => {
    setDeleted((d) => (d.includes(id) ? d : [...d, id]));
    setActiveWs((current) => (current === id ? null : current));
  }, []);

  return { workspaces, activeWs, setActiveWs: setActiveWs as (id: UUID | null) => void, dropWorkspace };
}
