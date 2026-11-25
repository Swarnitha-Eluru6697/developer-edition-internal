/**
 * Promise-based replacements for window.prompt / window.confirm.
 *
 * The browser's native dialogs always render top-centre and can't be styled
 * or moved. These render in the right-side Drawer like every other form in
 * the Workspace, and keep the call sites as simple as the native ones:
 *
 *   const name = await askText({ title: "New workspace", label: "Name" });
 *   if (!name) return;                       // null = cancelled
 *   if (!(await askConfirm({ title: "Delete file?", message: "…", danger: true }))) return;
 *
 * <DialogHost /> (./DialogHost.tsx) must be mounted once (src/app/App.tsx)
 * to render them. This module is the plain-TS request queue both sides share;
 * keeping it out of the .tsx keeps that file component-only (Fast Refresh).
 */

export interface TextRequest {
  kind: "text";
  title: string;
  label: string;
  hint?: string;
  defaultValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  icon?: string;
  resolve: (v: string | null) => void;
}

export interface ConfirmRequest {
  kind: "confirm";
  title: string;
  message: string;
  confirmLabel?: string;
  /** Red confirm button, for destructive actions. */
  danger?: boolean;
  /** When set, the confirm button stays disabled until this exact text is typed. */
  confirmText?: string;
  icon?: string;
  resolve: (v: boolean) => void;
}

/** "Do you want to save the changes you made to X?" — three outcomes, like a desktop editor. */
export type SaveChoice = "save" | "discard" | "cancel";

export interface SaveChangesRequest {
  kind: "save";
  title: string;
  message: string;
  resolve: (v: SaveChoice) => void;
}

export type DialogRequest = (TextRequest | ConfirmRequest | SaveChangesRequest) & { id?: number };

// One dialog at a time; a second call queues behind the first.
let queue: DialogRequest[] = [];
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
export const subscribeDialogs = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
export const currentDialog = () => queue[0] ?? null;

let nextId = 0;
function push(req: DialogRequest) { queue = [...queue, { ...req, id: ++nextId }]; emit(); }
export function popDialog() { queue = queue.slice(1); emit(); }

/** Ask for a line of text. Resolves to the trimmed value, or null if cancelled/empty. */
export function askText(opts: Omit<TextRequest, "kind" | "resolve">): Promise<string | null> {
  return new Promise((resolve) => push({ ...opts, kind: "text", resolve }));
}

/** Ask a yes/no question. Resolves true only on explicit confirmation. */
export function askConfirm(opts: Omit<ConfirmRequest, "kind" | "resolve">): Promise<boolean> {
  return new Promise((resolve) => push({ ...opts, kind: "confirm", resolve }));
}

/** Ask what to do with unsaved changes before closing `fileName`. Closing the dialog = "cancel". */
export function askSaveChanges(fileName: string): Promise<SaveChoice> {
  return new Promise((resolve) => push({
    kind: "save",
    title: `Save changes to ${fileName}?`,
    message: `Your changes to ${fileName} will be lost if you don't save them.`,
    resolve,
  }));
}
