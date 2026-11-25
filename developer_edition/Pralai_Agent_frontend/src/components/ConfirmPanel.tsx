import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import Modal from './Modal';

/** Ask before doing something irreversible, in a side panel.
 *
 * Replaces `window.confirm`. A browser dialog is a different visual
 * language from the rest of the app, cannot show the detail that makes
 * the decision answerable, and renders `\n` as the only formatting
 * available. This keeps the page visible behind it and can say what will
 * actually be destroyed.
 */
export default function ConfirmPanel({
  title, body, confirmLabel = 'Delete', onConfirm, onClose,
}: {
  title: string;
  body: React.ReactNode;
  confirmLabel?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Modal title={title} onClose={onClose} footer={<>
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button className="btn danger" disabled={busy}
        onClick={() => { setBusy(true); onConfirm(); }}>
        {busy ? 'Working…' : confirmLabel}
      </button>
    </>}>
      <div style={{ fontSize: 13, lineHeight: 1.6 }}>{body}</div>
    </Modal>
  );
}

/** Ask for confirmation in the right-side panel and wait for the answer:
 *  `if (!(await confirmAction({ title, body }))) return;`
 *
 *  The drop-in for `window.confirm`, which the browser draws as a centred
 *  popup in its own visual language. It mounts its own panel on <body>, so
 *  a caller needs no state or markup for it, and nothing it sits inside —
 *  a pinned toolbar, a scrolling frame, another panel — can clip it. */
export function confirmAction(opts: {
  title: string; body: React.ReactNode; confirmLabel?: string;
}): Promise<boolean> {
  return new Promise((resolve) => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    const finish = (ok: boolean) => {
      resolve(ok);
      // Unmounted after the click that answered it has finished rendering.
      setTimeout(() => { root.unmount(); host.remove(); });
    };
    root.render(
      <ConfirmPanel title={opts.title} body={opts.body} confirmLabel={opts.confirmLabel}
        onConfirm={() => finish(true)} onClose={() => finish(false)} />,
    );
  });
}
