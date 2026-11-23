/** The toast notification (uiSlice.notification); auto-dismisses after a few seconds. */
import { useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { dismissNotification } from "../store/uiSlice";
import type { AppDispatch, RootState } from "../store";

const AUTO_DISMISS_MS = 6000;

export function Toast() {
  const dispatch = useDispatch<AppDispatch>();
  const notification = useSelector((s: RootState) => s.ui.notification);

  useEffect(() => {
    if (!notification) return;
    const t = setTimeout(() => dispatch(dismissNotification()), AUTO_DISMISS_MS);
    return () => clearTimeout(t);
  }, [notification, dispatch]);

  if (!notification) return null;
  return (
    <div
      onClick={() => dispatch(dismissNotification())}
      style={{
        position: "fixed", top: 16, right: 16, zIndex: 2000, maxWidth: 420,
        display: "flex", alignItems: "flex-start", gap: 10, padding: "12px 14px",
        borderRadius: 8, cursor: "pointer", fontSize: 13, lineHeight: 1.4, color: "#fff",
        boxShadow: "0 10px 30px rgba(0,0,0,0.25)",
        background: notification.kind === "success" ? "#15803d"
          : notification.kind === "error" ? "#dc2626" : "#F97316",
      }}
    >
      <i className={`ti ${notification.kind === "success" ? "ti-circle-check"
        : notification.kind === "error" ? "ti-alert-circle" : "ti-info-circle"}`}
        style={{ fontSize: 18, flexShrink: 0, marginTop: 1 }} />
      <span>{notification.message}</span>
    </div>
  );
}
