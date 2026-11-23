/*
 * Bottom-right toast stack. Toasts are added/expired by AppContext.
 */
import React from "react";
import { useApp } from "../context/AppContext";

export default function Toasts() {
  const { toasts } = useApp();
  return (
    // FE-BUG-21: a screen reader announces anything added to a live region
    // without needing focus to move — role="status"/aria-live="polite" on
    // the container covers the common case (an ok/neutral toast shouldn't
    // interrupt whatever the user's doing); an error toast additionally
    // gets its own role="alert" (an implicit assertive live region), since
    // a failed action is worth interrupting for.
    <div className="toasts" role="status" aria-live="polite" aria-atomic="false">
      {toasts.map((t) => (
        <div className={`toast ${t.type}`} key={t.id} role={t.type === "err" ? "alert" : undefined}>{t.msg}</div>
      ))}
    </div>
  );
}
