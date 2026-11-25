import { useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { Icon } from "./Icons";

/** In-app replacement for window.confirm. The browser's own dialog always opens
 *  centred at the top and can't be styled; this opens as a right-hand drawer like
 *  every other panel in the app. Call it the same way: `if (!(await confirmDialog(…))) return;`
 *
 *  Self-contained on purpose: each call mounts its own panel and removes it when
 *  answered. An earlier version relied on one host component registering itself at
 *  start-up; a hot reload replaced the module but kept the old host, so calls found
 *  no host and fell back to the native dialog. There is no host to lose now, and no
 *  native fallback. */
export interface ConfirmOptions {
  title: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    const done = (ok: boolean) => {
      root.unmount();
      el.remove();
      resolve(ok);
    };
    root.render(<ConfirmPanel {...options} onDone={done} />);
  });
}

function ConfirmPanel({ title, message, confirmLabel, cancelLabel, danger, onDone }:
  ConfirmOptions & { onDone: (ok: boolean) => void }) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    confirmRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onDone(false); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); opener?.focus?.(); };
  }, [onDone]);
  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onDone(false); }}>
      <div className="drawer" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title">
        <div className="drawer-head">
          <div id="confirm-title" style={{ fontWeight: 700, fontSize: 16, color: "var(--ink)" }}>{title}</div>
          <button className="drawer-close" aria-label="Cancel" onClick={() => onDone(false)}><Icon name="x" /></button>
        </div>
        {message && (
          <div className="drawer-body">
            <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.6, color: "var(--text-muted)", whiteSpace: "pre-line" }}>{message}</p>
          </div>
        )}
        <div className="drawer-foot">
          <button className="btn btn-secondary" onClick={() => onDone(false)}>{cancelLabel ?? "Cancel"}</button>
          <button ref={confirmRef} className={danger ? "btn btn-danger-ghost" : "btn btn-primary"} onClick={() => onDone(true)}>
            {confirmLabel ?? "Confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}
