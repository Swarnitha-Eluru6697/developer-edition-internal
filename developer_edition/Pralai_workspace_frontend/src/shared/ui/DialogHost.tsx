/**
 * Renders the promise-based askText / askConfirm requests queued in
 * ./dialogStore in the right-side Drawer. Mount once (src/app/App.tsx).
 */
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Drawer, Field } from "./Drawer";
import { drawerBtn, drawerInput } from "./drawerStyles";
import {
  currentDialog, popDialog, subscribeDialogs,
  type ConfirmRequest, type DialogRequest, type SaveChangesRequest, type SaveChoice, type TextRequest,
} from "./dialogStore";

export function DialogHost() {
  const req = useSyncExternalStore(subscribeDialogs, currentDialog);
  // Keyed by request so each one gets fresh input state.
  return req ? <DialogDrawer key={req.id} req={req} /> : null;
}

function DialogDrawer({ req }: { req: DialogRequest }) {
  if (req.kind === "save") return <SaveChangesDrawer req={req} />;
  return <TextOrConfirmDrawer req={req} />;
}

/** Save / Don't Save / Cancel. The drawer's own close (×, Escape, backdrop) counts as Cancel. */
function SaveChangesDrawer({ req }: { req: SaveChangesRequest }) {
  const finish = (choice: SaveChoice) => { req.resolve(choice); popDialog(); };
  return (
    <Drawer
      title={req.title}
      icon="ti-alert-triangle"
      onClose={() => finish("cancel")}
      width={380}
      footer={<>
        <button autoFocus onClick={() => finish("save")} style={drawerBtn(true)}>Save</button>
        <button onClick={() => finish("discard")} style={drawerBtn(false)}>Don&apos;t Save</button>
        <button onClick={() => finish("cancel")} style={drawerBtn(false)}>Cancel</button>
      </>}
    >
      <div style={{ fontSize: 13, color: "#2E3D36", lineHeight: 1.55 }}>{req.message}</div>
    </Drawer>
  );
}

function TextOrConfirmDrawer({ req }: { req: TextRequest | ConfirmRequest }) {
  const [value, setValue] = useState(req.kind === "text" ? req.defaultValue ?? "" : "");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (req.kind === "text") { inputRef.current?.focus(); inputRef.current?.select(); }
  }, [req]);

  // Confirm dialogs with confirmText reuse `value` for the typed name.
  const blocked = req.kind === "text"
    ? !value.trim()
    : !!req.confirmText && value.trim() !== req.confirmText;

  useEffect(() => {
    if (req.kind === "confirm" && req.confirmText) inputRef.current?.focus();
  }, [req]);

  const finish = (ok: boolean) => {
    if (ok && blocked) return;
    if (req.kind === "text") req.resolve(ok ? value.trim() || null : null);
    else req.resolve(ok);
    popDialog();
  };

  const confirmStyle = {
    ...drawerBtn(true),
    ...(req.kind === "confirm" && req.danger ? { background: "#b23b3b" } : {}),
    ...(blocked ? { opacity: 0.5, cursor: "not-allowed" } : {}),
  };

  return (
    <Drawer
      title={req.title}
      icon={req.icon ?? (req.kind === "confirm" && req.danger ? "ti-alert-triangle" : req.kind === "text" ? "pi-pencil" : "ti-help-circle")}
      onClose={() => finish(false)}
      width={380}
      footer={<>
        <button
          onClick={() => finish(true)}
          disabled={blocked}
          style={confirmStyle}
        >
          {req.confirmLabel ?? (req.kind === "confirm" ? "Confirm" : "OK")}
        </button>
        <button onClick={() => finish(false)} style={drawerBtn(false)}>Cancel</button>
      </>}
    >
      {req.kind === "text" ? (
        <form onSubmit={(e) => { e.preventDefault(); if (value.trim()) finish(true); }}>
          <Field label={req.label} hint={req.hint}>
            <input
              ref={inputRef}
              value={value}
              placeholder={req.placeholder}
              onChange={(e) => setValue(e.target.value)}
              style={drawerInput}
            />
          </Field>
        </form>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); finish(true); }}>
          <div style={{ fontSize: 13, color: "#2E3D36", lineHeight: 1.55, marginBottom: req.confirmText ? 14 : 0 }}>{req.message}</div>
          {req.confirmText && (
            <Field label={`Type "${req.confirmText}" to confirm`}>
              <input
                ref={inputRef}
                value={value}
                placeholder={req.confirmText}
                onChange={(e) => setValue(e.target.value)}
                style={drawerInput}
              />
            </Field>
          )}
        </form>
      )}
    </Drawer>
  );
}
