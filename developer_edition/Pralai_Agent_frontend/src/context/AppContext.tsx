import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Me } from '../types';

export type DrawerType = 'agentProfile' | 'runDetail' | 'traceDetail' | 'alertDetail' | 'toolDetail' | 'envVars' | null;
export type ModalType = 'newAgent' | 'editAgent' | 'generateAgent' | 'publish' | 'addMcp' | 'apiKeyConfig' | 'dsConfig' | 'addEnvVar' | 'addKnowledgeBase' | 'newRagPipeline' | null | 'addProviderModel' | 'editKbConnection';

interface ToastMsg { id: number; text: string }

/** 'loading' until GET /api/me answers; 'error' when it failed. Both mean
 *  "no permissions": nothing is shown until the server says otherwise. */
export type PermissionsStatus = 'loading' | 'ready' | 'error';

interface AppState {
  // Who the gateway says this user is (GET /api/me, loaded once). Null
  // while loading or when the call failed.
  me: Me | null;
  /** The permissions every show/hide check uses: /api/me's, narrowed to the
   *  portal's "view as" role when it passed viewPermissions. */
  permissions: string[];
  permissionsStatus: PermissionsStatus;
  /** Does the user hold this permission? Unknown/loading/error = no. */
  can: (permission: string) => boolean;
  /** Does the user hold at least one of these permissions? */
  canAny: (permissions: readonly string[]) => boolean;

  // "Running as" chooser on the Web Apps page: picks which UCS principal
  // is sent as invoking_user, so the UCS-backed access check on published
  // apps can be exercised for any principal. An identity chooser, not
  // authentication and not a permission.
  endUserIdentity: string;
  setEndUserIdentity: (id: string) => void;

  toasts: ToastMsg[];
  toast: (text: string) => void;

  drawer: DrawerType;
  drawerArg: string | null;
  openDrawer: (type: DrawerType, arg?: string) => void;
  closeDrawer: () => void;

  modal: ModalType;
  modalArg: string | null;
  openModal: (type: ModalType, arg?: string) => void;
  closeModal: () => void;

  publishAgentId: string | null;
  setPublishAgentId: (id: string | null) => void;

  toolAction: string | null;
  setToolAction: (name: string | null) => void;

  // Bumped by a modal after it successfully writes something, and read
  // by pages as an effect dependency so they refetch.
  //
  // Modals are rendered by ModalHost, not by the page underneath, so a
  // save had no way to tell that page anything. The Integrations tabs
  // all load mount-only, so saving an API key toasted "saved", closed
  // the modal, and left the provider still listed under "Configuration
  // required" with an "Add API Key" button — indistinguishable from the
  // save having failed.
  dataVersion: number;
  notifyDataChanged: () => void;
}

const AppCtx = createContext<AppState | null>(null);

/** What the user is shown: /api/me's permissions, narrowed to the portal's
 *  "view as" choice when there is one (null/undefined = no narrowing). Only
 *  ever narrows — a permission /api/me did not grant never appears because
 *  viewPermissions lists it. UX only: the API still authorises every request. */
export function effectivePermissions(granted: readonly string[], viewPermissions?: readonly string[] | null): string[] {
  if (!viewPermissions) return [...granted];
  const view = new Set(viewPermissions);
  return granted.filter((p) => view.has(p));
}

export function AppProvider({ children, viewPermissions }: {
  children: React.ReactNode;
  /** The portal's "view as role" choice (see AgenticWorkflowApp). */
  viewPermissions?: string[] | null;
}) {
  const [me, setMe] = useState<Me | null>(null);
  const [permissionsStatus, setPermissionsStatus] = useState<PermissionsStatus>('loading');
  // Loaded once per mount. Anything other than a well-formed answer leaves
  // the user with no permissions — never a privileged default.
  useEffect(() => {
    let live = true;
    Promise.resolve()
      .then(() => api.me())
      .then((r) => {
        if (!live) return;
        if (r && Array.isArray(r.permissions)) {
          setMe({
            user: typeof r.user === 'string' ? r.user : null,
            userId: typeof r.userId === 'string' ? r.userId : null,
            tenant: typeof r.tenant === 'string' ? r.tenant : null,
            permissions: r.permissions.filter((x): x is string => typeof x === 'string'),
          });
          setPermissionsStatus('ready');
        } else {
          setMe(null);
          setPermissionsStatus('error');
        }
      })
      .catch(() => {
        if (!live) return;
        setMe(null);
        setPermissionsStatus('error');
      });
    return () => { live = false; };
  }, []);
  // Keyed on the contents, so a host re-render passing an equal new array
  // does not churn every consumer.
  const viewKey = viewPermissions ? JSON.stringify(viewPermissions) : null;
  const permissions = useMemo(
    () => effectivePermissions(me?.permissions ?? [], viewKey === null ? null : (JSON.parse(viewKey) as string[])),
    [me, viewKey],
  );
  const permissionSet = useMemo(() => new Set(permissions), [permissions]);
  const can = useCallback((p: string) => permissionSet.has(p), [permissionSet]);
  const canAny = useCallback((ps: readonly string[]) => ps.some((p) => permissionSet.has(p)), [permissionSet]);

  // Defaults to the real operator principal, not the invented
  // "user:alice". This value is what Publish sends as invoking_user, and
  // UCS decides against the groups that principal is actually a member
  // of — so a name UCS has never heard of can hold no grant and every
  // publish was refused on the invoking-user half regardless of how the
  // catalog was granted. Still a stand-in for the real login this
  // platform does not have (see the comment on the field below); the
  // chooser on the Web Apps page lists the principals UCS really has.
  const [endUserIdentity, setEndUserIdentity] = useState<string>('user:developer');
  const [toasts, setToasts] = useState<ToastMsg[]>([]);
  const [dataVersion, setDataVersion] = useState(0);
  const [drawer, setDrawer] = useState<DrawerType>(null);
  const [drawerArg, setDrawerArg] = useState<string | null>(null);
  const [modal, setModal] = useState<ModalType>(null);
  const [modalArg, setModalArg] = useState<string | null>(null);
  const [publishAgentId, setPublishAgentId] = useState<string | null>(null);
  const [toolAction, setToolAction] = useState<string | null>(null);

  const toast = useCallback((text: string) => {
    const id = Date.now();
    setToasts((t) => [...t, { id, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 2400);
  }, []);

  const openDrawer = useCallback((type: DrawerType, arg?: string) => {
    setDrawer(type);
    setDrawerArg(arg ?? null);
    setToolAction(null);
  }, []);
  const closeDrawer = useCallback(() => {
    setDrawer(null);
    setDrawerArg(null);
    setToolAction(null);
  }, []);

  const openModal = useCallback((type: ModalType, arg?: string) => {
    setModal(type);
    setModalArg(arg ?? null);
  }, []);
  const closeModal = useCallback(() => {
    setModal(null);
    setModalArg(null);
  }, []);
  const notifyDataChanged = useCallback(() => setDataVersion((v) => v + 1), []);

  return (
    <AppCtx.Provider value={{
      me, permissions, permissionsStatus, can, canAny,
      toasts, toast,
      endUserIdentity, setEndUserIdentity,
      drawer, drawerArg, openDrawer, closeDrawer,
      modal, modalArg, openModal, closeModal,
      publishAgentId, setPublishAgentId,
      toolAction, setToolAction,
      dataVersion, notifyDataChanged,
    }}>
      {children}
    </AppCtx.Provider>
  );
}

export function useApp() {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error('useApp must be used inside AppProvider');
  return ctx;
}
