/** App-shell side effects that aren't owned by any one feature. */
import { useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { isEmbedded } from "../../api/apiBase";
import { redirectToControlPlaneLogin } from "../../api/auth";
import { apiErrorStatus } from "../../api/errors";
import { usePermissionsState } from "../../rbac/hooks";
import { useLatest } from "../../shared/hooks/useLatest";
import { maxChatWidth, notify, openGitModal, saveNavSection, setChatWidth, setNavSection, toggleChat } from "../../store/uiSlice";
import type { AppDispatch, RootState } from "../../store";
import { isLocalDev } from "../env";
import { canOpenSection } from "../navigation";

/** Redirect to the control-plane login on 401 — but never in local dev, where
 *  that URL is a dead end, nor inside the portal, which owns sign-in itself;
 *  AuthErrorScreen explains the failure instead. */
export function useUnauthorizedRedirect(meError: unknown) {
  useEffect(() => {
    if (apiErrorStatus(meError) === 401 && !isLocalDev() && !isEmbedded()) redirectToControlPlaneLogin();
  }, [meError]);
}

/** If the caller's permissions don't open the active section (e.g. a page restored
 *  from a previous session, or a permission removed since) fall back to Workspace
 *  rather than keep rendering a section the sidebar no longer lists. */
export function useNavGuard() {
  const dispatch = useDispatch<AppDispatch>();
  const navSection = useSelector((s: RootState) => s.ui.navSection);
  const { permissions, status } = usePermissionsState();
  useEffect(() => {
    // Until /api/v1/me has answered the permissions are unknown, and every gated section would look
    // forbidden — don't bounce a restored page (e.g. dbt after a refresh) to Workspace.
    if (status !== "ready") return;
    if (!canOpenSection(navSection, permissions)) dispatch(setNavSection("ws"));
  }, [status, navSection, permissions, dispatch]);
}

/** Remember the active page so a refresh stays on it (see uiSlice.readStoredNavSection). */
export function usePersistNavSection() {
  const navSection = useSelector((s: RootState) => s.ui.navSection);
  useEffect(() => saveNavSection(navSection), [navSection]);
}

/** Shrinking the window can leave the chat wider than there's room for, which
 *  would squeeze the editor to nothing — re-clamp on resize. Not persisted: a
 *  small window is a temporary squeeze, not a preference change. */
export function useChatWidthClamp() {
  const dispatch = useDispatch<AppDispatch>();
  const showChat = useSelector((s: RootState) => s.ui.showChat);
  const chatWidth = useSelector((s: RootState) => s.ui.chatWidth);
  useEffect(() => {
    if (!showChat) return;
    const fn = () => {
      const max = maxChatWidth();
      if (chatWidth > max) dispatch(setChatWidth(max));
    };
    window.addEventListener("resize", fn);
    return () => window.removeEventListener("resize", fn);
  }, [showChat, chatWidth, dispatch]);
}

interface ShortcutHandlers {
  /** False for notebooks: they own their per-cell Run/Save, and the editor
   *  shortcuts would submit an invalid notebook-cell request. */
  editorShortcuts: boolean;
  /** ⌘S saves whichever file is open — a notebook included, which has no ⌘↵ of its own at this level. */
  onSave: () => void;
  onRun: () => void;
}

/** ⌘S save, ⌘↵ run/validate, ⌘K toggle the codegen chat. Registered once;
 *  always calls the latest handlers. */
export function useKeyboardShortcuts(handlers: ShortcutHandlers) {
  const dispatch = useDispatch<AppDispatch>();
  const latest = useLatest(handlers);
  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      const { editorShortcuts, onSave, onRun } = latest.current;
      if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); onSave(); }
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); if (editorShortcuts) onRun(); }
      if ((e.metaKey || e.ctrlKey) && e.key === "k") { e.preventDefault(); dispatch(toggleChat()); }
    };
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  }, [dispatch, latest]);
}


/**
 * Return leg of the Git OAuth sign-in. The provider redirects to the backend
 * callback, which stores the token and sends the browser to
 * /?git_connected=<provider> (or /?git_connect_error=<message>). Show the
 * outcome, reopen the "From Git repository" dialog (its form was saved to
 * sessionStorage before leaving), and strip the parameter from the URL.
 */
export function useGitOAuthReturn() {
  const dispatch = useDispatch<AppDispatch>();
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("git_connected");
    const error = params.get("git_connect_error");
    if (!connected && !error) return;
    dispatch(connected
      ? notify({ kind: "success", message: `Connected to ${connected} — you can create the workspace now.` })
      : notify({ kind: "error", message: `Git sign-in failed: ${error}` }));
    dispatch(openGitModal());
    params.delete("git_connected");
    params.delete("git_connect_error");
    const qs = params.toString();
    window.history.replaceState(null, "", window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash);
  }, [dispatch]);
}

