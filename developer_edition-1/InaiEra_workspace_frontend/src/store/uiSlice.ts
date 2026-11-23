/**
 * UI state slice — tracks which panels are open, active nav section,
 * open file tabs, and modals. Keeps App.tsx clean.
 */
import { createSlice, PayloadAction } from "@reduxjs/toolkit";
import type { UUID } from "../types";
import { oneOf, readPersisted } from "../shared/usePersistedState";

export type NavSection = "ws" | "dc" | "dbt" | "sqljobs" | "pythonjobs" | "flink" | "dq" | "alerts" | "catalog" | "intel" | "settings";
export type FilePanelTab = "files" | "changes";

// Which tab of the file panel (Files / Changes) is open is remembered across a refresh like the section is (FilePanelTabs saves it).
export const FILE_PANEL_TAB_KEY = "ws.filePanelTab";
export function readStoredFilePanelTab(): FilePanelTab {
  return readPersisted<FilePanelTab>(FILE_PANEL_TAB_KEY, "files", oneOf<FilePanelTab>("files", "changes"));
}

// ── Active page ─────────────────────────────────────────────────────────
// The active sidebar section lives only in memory, so a refresh used to drop
// back to Workspace. Remember it per tab (sessionStorage): a refresh keeps the
// page, a new tab still starts on Workspace.
const NAV_SECTION_KEY = "inaiera.navSection";
const NAV_SECTION_IDS: readonly NavSection[] = ["ws", "dc", "dbt", "sqljobs", "pythonjobs", "flink", "dq", "alerts", "catalog", "intel", "settings"];

export function readStoredNavSection(): NavSection {
  try {
    const v = window.sessionStorage.getItem(NAV_SECTION_KEY);
    return NAV_SECTION_IDS.includes(v as NavSection) ? (v as NavSection) : "ws";
  } catch {
    return "ws"; // storage unavailable (private mode)
  }
}

export function saveNavSection(section: NavSection): void {
  try {
    window.sessionStorage.setItem(NAV_SECTION_KEY, section);
  } catch {
    /* ignore — the page still applies for this session */
  }
}

// ── File tree: which folders are open ───────────────────────────────────
// Kept in the store (not in each tree node) so it survives the Files tab being
// unmounted when you switch to Changes or to another page. Keyed by
// "<workspaceId>:<folder path>"; a folder with no entry uses its default (open
// for the top two levels). Also remembered per browser tab across a refresh.
const TREE_OPEN_KEY = "inaiera.treeOpen";
export type TreeOpenMap = Record<string, boolean>;

export const treeNodeKey = (workspaceId: string, path: string): string => `${workspaceId}:${path}`;

export function readStoredTreeOpen(): TreeOpenMap {
  try {
    const raw = JSON.parse(window.sessionStorage.getItem(TREE_OPEN_KEY) ?? "{}");
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    return Object.fromEntries(Object.entries(raw).filter(([, v]) => typeof v === "boolean")) as TreeOpenMap;
  } catch {
    return {}; // storage unavailable or corrupt
  }
}

export function saveTreeOpen(map: TreeOpenMap): void {
  try {
    window.sessionStorage.setItem(TREE_OPEN_KEY, JSON.stringify(map));
  } catch {
    /* ignore — the state still applies for this session */
  }
}

// ── Codegen chat width ──────────────────────────────────────────────────
// A layout preference, so it outlives the session. Persisted to localStorage
// rather than the backend: it's per-device (depends on screen size), not
// per-account.
export const CHAT_WIDTH_DEFAULT = 285;
export const CHAT_WIDTH_MIN = 240;
const CHAT_WIDTH_KEY = "workspace_chat_width";
// Fixed shell chrome (rail + product nav + file panel) and the narrowest
// editor worth keeping. One definition, used by both the loader and the drag.
const SHELL_CHROME_PX = 52 + 195 + 225;
const MIN_EDITOR_PX = 320;

/** Widest the chat may be without squeezing the editor below MIN_EDITOR_PX. */
export function maxChatWidth(): number {
  const avail = (typeof window === "undefined" ? 1440 : window.innerWidth) - SHELL_CHROME_PX - MIN_EDITOR_PX;
  return Math.max(CHAT_WIDTH_MIN, avail);
}

export const clampChatWidth = (n: number): number =>
  Math.round(Math.min(Math.max(n, CHAT_WIDTH_MIN), maxChatWidth()));

function readStoredChatWidth(): number {
  try {
    const n = Number(window.localStorage.getItem(CHAT_WIDTH_KEY));
    // Junk, or a width saved on a much wider monitor, must not be trusted —
    // clamp to what actually fits here rather than blowing out the layout.
    return Number.isFinite(n) && n >= CHAT_WIDTH_MIN ? clampChatWidth(n) : CHAT_WIDTH_DEFAULT;
  } catch {
    return CHAT_WIDTH_DEFAULT; // storage unavailable (private mode)
  }
}

/**
 * Write the width to localStorage. Called on drag END, not on every mousemove
 * — a synchronous storage write per frame would make the drag stutter.
 */
export function saveChatWidth(width: number): void {
  try {
    window.localStorage.setItem(CHAT_WIDTH_KEY, String(Math.round(width)));
  } catch {
    /* ignore — the width still applies for this session */
  }
}

interface UIState {
  navSection: NavSection;
  filePanelTab: FilePanelTab;
  showChat: boolean;
  showDbtPanel: boolean;
  openTabs: UUID[];
  activeTab: UUID | null;
  gitModalOpen: boolean;
  connectionModalOpen: boolean;
  editingConnectionId: UUID | null;
  // Set by flows that create a workspace (e.g. the Git modal) so the shell
  // can switch to the freshly-created workspace once the list refetches.
  pendingWorkspace: UUID | null;
  // A job picked in the header search: the Jobs page it lives on opens it, then clears this.
  openJob: { section: NavSection; id: UUID } | null;
  // Transient toast notification (create result, errors, …).
  notification: { kind: "success" | "error" | "info"; message: string } | null;
  // Every toast of this session, newest first (capped), for the header bell.
  notificationLog: NotificationEntry[];
  unreadNotifications: number;
  // The DB connection selected in the Database Explorer — SQL editor runs
  // against this connection's database.
  activeConnectionId: UUID | null;
  // SQL editor bottom panel: the results table can be closed, and the query
  // history sidebar can be toggled in its place.
  resultsOpen: boolean;
  historyOpen: boolean;
  // Bridge from the codegen chat to NotebookView: when the user clicks
  // "Insert into cell" and a notebook is open, the code lands here and
  // NotebookView appends a cell for it, then clears this.
  pendingCellInsert: { code: string; language: "sql" | "python" } | null;
  // Width of the codegen chat panel, restored from localStorage on load.
  chatWidth: number;
  // Folder open/closed state of the file tree, see treeNodeKey().
  treeOpen: TreeOpenMap;
}

export interface NotificationEntry { id: number; kind: "success" | "error" | "info"; message: string; at: number }
const NOTIFICATION_LOG_MAX = 30;
let notificationSeq = 0;

const initialState: UIState = {
  navSection: readStoredNavSection(),
  filePanelTab: readStoredFilePanelTab(),
  showChat: false,
  showDbtPanel: false,
  openTabs: [],
  activeTab: null,
  gitModalOpen: false,
  connectionModalOpen: false,
  editingConnectionId: null,
  pendingWorkspace: null,
  openJob: null,
  notification: null,
  notificationLog: [],
  unreadNotifications: 0,
  activeConnectionId: null,
  resultsOpen: true,
  historyOpen: false,
  pendingCellInsert: null,
  chatWidth: readStoredChatWidth(),
  treeOpen: readStoredTreeOpen(),
};

export const uiSlice = createSlice({
  name: "ui",
  initialState,
  reducers: {
    setNavSection(state, action: PayloadAction<NavSection>) { state.navSection = action.payload; },
    setFilePanelTab(state, action: PayloadAction<FilePanelTab>) { state.filePanelTab = action.payload; },
    toggleChat(state) { state.showChat = !state.showChat; },
    setShowChat(state, action: PayloadAction<boolean>) { state.showChat = action.payload; },
    toggleDbtPanel(state) { state.showDbtPanel = !state.showDbtPanel; },
    openTab(state, action: PayloadAction<UUID>) {
      if (!state.openTabs.includes(action.payload)) state.openTabs.push(action.payload);
      state.activeTab = action.payload;
    },
    closeTab(state, action: PayloadAction<UUID>) {
      state.openTabs = state.openTabs.filter(id => id !== action.payload);
      if (state.activeTab === action.payload) {
        state.activeTab = state.openTabs[state.openTabs.length - 1] ?? null;
      }
    },
    setTreeNodeOpen(state, action: PayloadAction<{ key: string; open: boolean }>) {
      state.treeOpen[action.payload.key] = action.payload.open;
    },
    setActiveTab(state, action: PayloadAction<UUID>) { state.activeTab = action.payload; },
    // After a refresh: reopen the files that were open (only when nothing is open yet, so it can never undo what you just did).
    restoreTabs(state, action: PayloadAction<{ openTabs: UUID[]; activeTab: UUID | null }>) {
      if (state.openTabs.length > 0) return;
      state.openTabs = action.payload.openTabs;
      state.activeTab = action.payload.activeTab;
    },
    clearActiveTab(state) { state.activeTab = null; },
    // The workspace they belong to was deleted: nothing is open any more.
    closeAllTabs(state) { state.openTabs = []; state.activeTab = null; },
    openGitModal(state) { state.gitModalOpen = true; },
    closeGitModal(state) { state.gitModalOpen = false; },
    openConnectionModal(state, action: PayloadAction<UUID | null>) {
      state.connectionModalOpen = true;
      state.editingConnectionId = action.payload;
    },
    closeConnectionModal(state) {
      state.connectionModalOpen = false;
      state.editingConnectionId = null;
    },
    setPendingWorkspace(state, action: PayloadAction<UUID>) { state.pendingWorkspace = action.payload; },
    clearPendingWorkspace(state) { state.pendingWorkspace = null; },
    showJob(state, action: PayloadAction<{ section: NavSection; id: UUID }>) { state.navSection = action.payload.section; state.openJob = action.payload; },
    clearOpenJob(state) { state.openJob = null; },
    notify: {
      reducer(state, action: PayloadAction<{ kind: "success" | "error" | "info"; message: string }, string, { id: number; at: number }>) {
        state.notification = action.payload;
        state.notificationLog = [{ ...action.payload, ...action.meta }, ...state.notificationLog].slice(0, NOTIFICATION_LOG_MAX);
        state.unreadNotifications = Math.min(state.unreadNotifications + 1, NOTIFICATION_LOG_MAX);
      },
      // Id and timestamp are made here so the reducer stays pure.
      prepare(payload: { kind: "success" | "error" | "info"; message: string }) {
        return { payload, meta: { id: ++notificationSeq, at: Date.now() } };
      },
    },
    markNotificationsRead(state) { state.unreadNotifications = 0; },
    removeNotification(state, action: PayloadAction<number>) { state.notificationLog = state.notificationLog.filter((n) => n.id !== action.payload); },
    clearNotifications(state) { state.notificationLog = []; state.unreadNotifications = 0; },
    dismissNotification(state) { state.notification = null; },
    setActiveConnection(state, action: PayloadAction<UUID | null>) { state.activeConnectionId = action.payload; },
    setResultsOpen(state, action: PayloadAction<boolean>) { state.resultsOpen = action.payload; },
    closeResults(state) { state.resultsOpen = false; },
    setHistoryOpen(state, action: PayloadAction<boolean>) { state.historyOpen = action.payload; },
    toggleHistory(state) { state.historyOpen = !state.historyOpen; },
    requestCellInsert(state, action: PayloadAction<{ code: string; language: "sql" | "python" }>) { state.pendingCellInsert = action.payload; },
    clearCellInsert(state) { state.pendingCellInsert = null; },
    // Kept pure: persistence is the caller's job via saveChatWidth(), so a
    // drag can update state every frame without touching localStorage.
    setChatWidth(state, action: PayloadAction<number>) { state.chatWidth = action.payload; },
  },
});

export const {
  setNavSection, setFilePanelTab, setTreeNodeOpen, toggleChat, setShowChat, toggleDbtPanel,
  openTab, closeTab, closeAllTabs, setActiveTab, restoreTabs, clearActiveTab, openGitModal, closeGitModal,
  openConnectionModal, closeConnectionModal,
  setPendingWorkspace, clearPendingWorkspace, showJob, clearOpenJob,
  notify, dismissNotification, markNotificationsRead, removeNotification, clearNotifications, setActiveConnection,
  setResultsOpen, closeResults, setHistoryOpen, toggleHistory,
  requestCellInsert, clearCellInsert,
  setChatWidth,
} = uiSlice.actions;

export default uiSlice.reducer;
