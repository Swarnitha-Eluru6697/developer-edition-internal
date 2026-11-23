/*
 * App-wide facade: config, toasts, health and the drawer modal.
 *
 * Config and toasts are real shared async state, so they live in the Redux
 * store (src/store) — this file just wires up <ReduxProvider> and exposes
 * them through the same `useApp()` shape pages already used, to keep the
 * migration to Redux Toolkit + RTK Query from touching every call site.
 *
 * The drawer modal is the one thing that deliberately stays outside Redux:
 * it holds a live React node (with closures/callbacks), and the Redux docs
 * are explicit that non-serializable values like React elements don't belong
 * in the store. It's ephemeral, single-consumer UI state, not shared async
 * data, so a small local Context is the correct tool here.
 */
import React, { createContext, useContext, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Provider as ReduxProvider } from "react-redux";
import { createDataConnectStore, type DataConnectStore } from "../store/store";
import { useAppDispatch, useAppSelector } from "../store/hooks";
import { setConfig, selectConfig } from "../store/configSlice";
import { showToast, selectToasts } from "../store/notificationsSlice";
import { useGetHealthQuery, useGetMeQuery } from "../store/apiSlice";
import type { ConfigState } from "../store/configSlice";
import type { Toast } from "../store/notificationsSlice";
import type { ApiOperation } from "../api";
import { errorMessage } from "../errors";
import { CAN_NOTHING, effectivePermissions, permissionChecker, type Can, type Me } from "../permissions";
import { RUNTIME_MODE, type RuntimeMode } from "../runtimeMode";
import ConfigGate from "../components/ConfigGate";

// Standalone-dev persisted-config key; the dev DEFAULT (and the FEDERATED requirement) live in ../runtimeConfig (FE-GAP-01).
const LS_BASE = "dc_base_url";

// FE-BUG-02: RUNTIME_MODE (see ../runtimeMode) is the one explicit boundary
// that decides whether the localStorage key above is ever read —
// DEV_CONFIG_ENABLED is just this file's local shorthand for "we're in
// STANDALONE_DEV". Settings' "point this app at any backend URL, persisted in
// localStorage" behavior is a dev convenience for iterating against a local
// backend without rebuilding; it's deliberately unavailable in any actual
// built artifact, so a production build can never be silently repointed at an
// arbitrary backend by whoever loads the page (see Settings.tsx and saveCfg()).
//
// Who the user is and what they may do is NEVER local state: it is GET /me
// (the backend's view of the signed ticket), in every mode.
const DEV_CONFIG_ENABLED = RUNTIME_MODE === "STANDALONE_DEV";

// ── Modal (drawer) context — intentionally not in Redux, see file header ────
interface ModalState { node: ReactNode; wide: boolean }
// FE-BUG-13: a form open in the drawer can register a "close guard" — a
// function returning whether it currently has unsaved changes. closeModal()
// is the ONE chokepoint every close path already goes through (the Drawer's
// × icon, the backdrop click in Modal.tsx, and a form's own footer Cancel
// button all call it, directly or via the `onClose` prop each form receives
// — see e.g. Pipelines.tsx's `openModal(<PipelineForm onClose={closeModal} />)`),
// so guarding it here covers all of them at once instead of needing to
// re-thread a confirmation through every place that can close the drawer.
// A guarded close doesn't close — it raises `closePrompt` for Modal.tsx to
// render an "Unsaved changes" confirmation over the still-mounted form
// (so "Continue editing" has actual form state to return to);
// `discardAndClose` bypasses the guard entirely, for the confirmation's own
// "Discard" button and for a form's intentional post-save close.
interface ModalContextValue {
  modal: ModalState | null;
  modalOn: boolean;
  openModal: (node: ReactNode, opts?: { wide?: boolean }) => void;
  closeModal: () => void;
  setCloseGuard: (guard: (() => boolean) | null, message?: string) => void;
  closePrompt: { message: string } | null;
  cancelClose: () => void;
  discardAndClose: () => void;
}
const ModalCtx = createContext<ModalContextValue | null>(null);

function ModalProvider({ children }: { children: ReactNode }) {
  const [modal, setModal] = useState<ModalState | null>(null);
  const [modalOn, setModalOn] = useState(false);
  const [closePrompt, setClosePrompt] = useState<{ message: string } | null>(null);
  const guardRef = useRef<(() => boolean) | null>(null);
  const guardMessageRef = useRef<string>("You have unsaved changes.");

  const openModal = useCallback((node: ReactNode, { wide = false }: { wide?: boolean } = {}) => {
    // The drawer appears in place, already open: no slide-in, and no frame where it exists but is not yet "on".
    setModal({ node, wide });
    setClosePrompt(null);
    setModalOn(true);
  }, []);

  const discardAndClose = useCallback(() => {
    guardRef.current = null;
    setClosePrompt(null);
    setModalOn(false);
    setModal(null);                                   // gone at once: there is no slide-out to wait for
  }, []);

  const closeModal = useCallback(() => {
    if (guardRef.current && guardRef.current()) {
      setClosePrompt({ message: guardMessageRef.current });
      return;
    }
    discardAndClose();
  }, [discardAndClose]);

  const setCloseGuard = useCallback((guard: (() => boolean) | null, message?: string) => {
    guardRef.current = guard;
    if (message) guardMessageRef.current = message;
  }, []);

  const cancelClose = useCallback(() => setClosePrompt(null), []);

  // Browser tab close/refresh — checked live against whatever the currently
  // open form's guard reports at the moment of unload, not a value captured
  // when the guard was registered, so a discard-then-clean-edit in the same
  // session is never stuck warning about changes that no longer exist.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (guardRef.current && guardRef.current()) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);

  const value = useMemo(
    () => ({ modal, modalOn, openModal, closeModal, setCloseGuard, closePrompt, cancelClose, discardAndClose }),
    [modal, modalOn, openModal, closeModal, setCloseGuard, closePrompt, cancelClose, discardAndClose],
  );
  return <ModalCtx.Provider value={value}>{children}</ModalCtx.Provider>;
}

function useModal(): ModalContextValue {
  const ctx = useContext(ModalCtx);
  if (!ctx) throw new Error("useModal must be used within ModalProvider");
  return ctx;
}

// ── useApp() facade ──────────────────────────────────────────────────────────
interface HealthState { state: "checking" | "online" | "offline"; service: string }
/** Where the caller's permissions stand: "loading" until GET /me answers; "error" if it failed (then nothing is allowed). */
export type AccessState = "loading" | "ready" | "error";
export interface AppContextValue {
  cfg: ConfigState;
  saveCfg: (next: { baseUrl?: string }) => void;
  /** True only when saveCfg() can actually change the backend — standalone dev builds, never embedded or production. */
  configEditable: boolean;
  /** GET /me — the signed-in user, tenant and permissions; null until it answers (or if it failed). */
  me: Me | null;
  access: AccessState;
  /** Why /me failed (shown to the user), or null. */
  accessError: string | null;
  /** Does the caller hold this permission? False for everything until /me has answered. UI hiding only — the API enforces.
   *  Already narrowed to the host's `viewPermissions` ("view as role"): /me ∩ viewPermissions, never wider than /me. Every
   *  show/hide check reads this, so the narrowing lives here and nowhere else. */
  can: Can;
  /** True while the host narrows the view to one of the user's roles (`viewPermissions` given) — for wording only. */
  viewingAs: boolean;
  refetchMe: () => void;
  hostControlled: boolean;
  /** FE-BUG-02: FEDERATED or STANDALONE_DEV — see ../runtimeMode. Only STANDALONE_DEV ever reads localStorage for backend config. */
  runtimeMode: RuntimeMode;
  toasts: Toast[];
  toast: (msg: string, type?: string) => void;
  ok: (m: string) => void;
  err: (e: unknown) => void;
  modal: ModalState | null;
  modalOn: boolean;
  openModal: (node: ReactNode, opts?: { wide?: boolean }) => void;
  closeModal: () => void;
  /** FE-BUG-13: register (or clear, with `null`) the currently-open drawer
   * form's "do I have unsaved changes" check — see useUnsavedChangesGuard. */
  setCloseGuard: (guard: (() => boolean) | null, message?: string) => void;
  /** Non-null while the "Unsaved changes" confirmation is up over the
   * still-open drawer. */
  closePrompt: { message: string } | null;
  /** Dismiss the confirmation and keep the drawer/form open as-is. */
  cancelClose: () => void;
  /** Close the drawer unconditionally, bypassing any registered guard. */
  discardAndClose: () => void;
  health: HealthState;
  checkHealth: () => void;
}
const AppCtx = createContext<AppContextValue | null>(null);
export function useApp(): AppContextValue {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error("useApp must be used within AppProvider");
  return ctx;
}

function InnerApp({ hostControlled, viewPermissions, children }: { hostControlled: boolean; viewPermissions?: readonly string[] | null; children: ReactNode }) {
  const dispatch = useAppDispatch();
  const storedCfg = useAppSelector(selectConfig);
  const toasts = useAppSelector(selectToasts);
  const { modal, modalOn, openModal, closeModal, setCloseGuard, closePrompt, cancelClose, discardAndClose } = useModal();
  const { data: healthData, isLoading: healthLoading, isError: healthError, refetch: checkHealth } = useGetHealthQuery();
  const { data: meData, isLoading: meLoading, isFetching: meFetching, error: meError, refetch: refetchMe } = useGetMeQuery();

  const toast = useCallback((msg: string, type = "") => { dispatch(showToast(msg, type)); }, [dispatch]);
  const ok = useCallback((m: string) => toast(m, "ok"), [toast]);
  const err = useCallback((e: unknown) => toast(errorMessage(e), "err"), [toast]);

  const saveCfg = useCallback((next: { baseUrl?: string }) => {
    // Host-provided config is authoritative when embedded: this is a hard
    // no-op, not just a skipped localStorage write, so nothing — Settings'
    // own disabled inputs, a future call site, a bug — can override it at
    // the state layer even transiently.
    if (hostControlled) return;
    // Standalone but not a dev build: config is fixed for this deployment,
    // not user-editable (see DEV_CONFIG_ENABLED above).
    if (!DEV_CONFIG_ENABLED) return;
    if (typeof localStorage !== "undefined" && next.baseUrl != null) localStorage.setItem(LS_BASE, next.baseUrl);
    dispatch(setConfig(next));
  }, [dispatch, hostControlled]);

  // The tenant shown in the UI (and scoping local caches) is the one the backend reported — never chosen here.
  const me = meError ? null : (meData ?? null);
  useEffect(() => {
    if (me && typeof me.tenant === "string") dispatch(setConfig({ orgId: me.tenant }));
  }, [dispatch, me]);

  // Pages read the tenant from here: the backend's answer, in the very first render that has one (no frame with a blank tenant).
  const cfg = useMemo<ConfigState>(() => (me && typeof me.tenant === "string" ? { ...storedCfg, orgId: me.tenant } : storedCfg), [storedCfg, me]);
  // "View as role": the host re-renders with a new viewPermissions when the user switches; keyed on its contents (not the array's
  // identity, which a host may rebuild every render) so `can` changes exactly when the chosen role does — no remount, no reload.
  const viewing = Array.isArray(viewPermissions);
  const viewKey = viewing ? JSON.stringify(viewPermissions) : null;
  const view = useMemo<string[] | null>(() => (viewKey === null ? null : (JSON.parse(viewKey) as string[])), [viewKey]);
  const can = useMemo<Can>(() => (me ? permissionChecker(effectivePermissions(me.permissions, view)) : CAN_NOTHING), [me, view]);
  const access: AccessState = meError ? "error" : (meLoading || (meFetching && !meData)) ? "loading" : me ? "ready" : "loading";
  const accessError = meError ? errorMessage(meError) : null;

  const health: HealthState = healthLoading ? { state: "checking", service: "" }
    : healthError ? { state: "offline", service: "" }
    : { state: "online", service: (healthData && healthData.service) || "" };

  const value: AppContextValue = {
    cfg, saveCfg, configEditable: !hostControlled && DEV_CONFIG_ENABLED,
    me, access, accessError, can, viewingAs: viewing, refetchMe, hostControlled, runtimeMode: RUNTIME_MODE,
    toasts, toast, ok, err,
    modal, modalOn, openModal, closeModal, setCloseGuard, closePrompt, cancelClose, discardAndClose,
    health, checkHealth,
  };

  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}

export interface AppProviderProps {
  apiBase?: string;
  /** The host's "view as role" narrowing (permission IDs); null/undefined = none. See DataConnectAppProps.viewPermissions. */
  viewPermissions?: readonly string[] | null;
  onOperation?: (op: ApiOperation) => void;
  children: ReactNode;
}

export function AppProvider({ onError, ...props }: AppProviderProps & { onError?: (error: Error) => void }) {
  // FE-GAP-01: the backend URL is resolved by runtimeConfig.ts. In FEDERATED (production) mode the host MUST supply
  // `apiBase` — if it is missing this renders a configuration error instead of the app, so nothing is built that could
  // act against the development backend. Only a dev build (STANDALONE_DEV) falls back to what Settings last persisted
  // and then to the local default; a production build never reads that stored value at all — see DEV_CONFIG_ENABLED.
  const storedBase = DEV_CONFIG_ENABLED && typeof localStorage !== "undefined" ? localStorage.getItem(LS_BASE) : null;
  return (
    <ConfigGate mode={RUNTIME_MODE} apiBase={props.apiBase} storedBase={storedBase} onError={onError}>
      {(config) => <ConfiguredAppProvider {...props} initialBase={config.baseUrl} hostControlled={config.hostControlled} />}
    </ConfigGate>
  );
}

interface ConfiguredProps extends AppProviderProps {
  /** Already resolved and validated by ConfigGate — never a development default in FEDERATED mode. */
  initialBase: string;
  hostControlled: boolean;
}

function ConfiguredAppProvider({ apiBase, onOperation, viewPermissions, children, initialBase, hostControlled }: ConfiguredProps) {
  // Stable identity so a re-render with a new host callback doesn't require
  // rebuilding the whole store (and resetting the RTK Query cache).
  const onOperationRef = useRef(onOperation);
  onOperationRef.current = onOperation;
  const stableOnOperation = useCallback((op: ApiOperation) => { onOperationRef.current && onOperationRef.current(op); }, []);

  const storeRef = useRef<DataConnectStore | null>(null);
  if (!storeRef.current) {
    storeRef.current = createDataConnectStore({ baseUrl: initialBase, orgId: "", onOperation: stableOnOperation });
  }
  const store = storeRef.current;

  // Keep in sync if the host later changes the backend route.
  useEffect(() => {
    // The same trimmed string ConfigGate validated; a blank or non-string prop is "not provided" and changes nothing.
    const trimmed = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : undefined);
    store.dispatch(setConfig({ baseUrl: trimmed(apiBase) }));
  }, [store, apiBase]);

  return (
    <ReduxProvider store={store}>
      <ModalProvider>
        <InnerApp hostControlled={hostControlled} viewPermissions={viewPermissions}>{children}</InnerApp>
      </ModalProvider>
    </ReduxProvider>
  );
}
