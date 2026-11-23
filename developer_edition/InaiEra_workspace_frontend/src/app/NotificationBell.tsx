/**
 * Header bell, like the other InAiEra apps' top bar. Workspace has no Alerts
 * page, so it lists this session's notifications — every toast raised through
 * uiSlice.notify (runs, saves, Git, errors) — newest first, with an unread dot. Five at a time: Prev / Next page through them, a row
 * shows its own delete button on hover (like the file tree), and "Delete all" empties the list.
 * Standalone only: hosted by the Control Portal (it passes `roleBadges`), the portal rail carries the bell (AppHeader).
 */
import { useRef, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useClickOutside } from "../shared/hooks/useClickOutside";
import { clearNotifications, markNotificationsRead, removeNotification, type NotificationEntry } from "../store/uiSlice";
import type { AppDispatch, RootState } from "../store";
import { Glyph } from "../shared/ui/Glyph";
import { ACTION_ICON, IconAction } from "../shared/ui/IconAction";

const KIND: Record<NotificationEntry["kind"], { icon: string; color: string; bg: string }> = {
  success: { icon: "ti-circle-check", color: "#15803D", bg: "#F0FDF4" },
  error: { icon: "ti-alert-circle", color: "#B91C1C", bg: "#FEF2F2" },
  info: { icon: "ti-info-circle", color: "#C2410C", bg: "#FFF7ED" },
};

const PAGE_SIZE = 5;

const footerButton = (danger = false, disabled = false): React.CSSProperties => ({
  height: 26, padding: "0 10px", fontSize: 12, borderRadius: 6, background: "#fff", cursor: disabled ? "not-allowed" : "pointer",
  border: `1px solid ${danger ? "#FECACA" : "#E5E7EB"}`, color: danger ? "#B91C1C" : "#374151", opacity: disabled ? 0.45 : 1,
});

function ago(at: number, now: number): string {
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export function NotificationBell({ buttonStyle }: { buttonStyle: React.CSSProperties }) {
  const dispatch = useDispatch<AppDispatch>();
  const log = useSelector((s: RootState) => s.ui.notificationLog);
  const unread = useSelector((s: RootState) => s.ui.unreadNotifications);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [hovered, setHovered] = useState<number | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, open, () => setOpen(false));
  const now = Date.now();
  // Deleting the last row of the last page must not leave an empty page behind.
  const pages = Math.max(1, Math.ceil(log.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const shown = log.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);

  const toggle = () => {
    if (!open) { dispatch(markNotificationsRead()); setPage(0); }
    setOpen(!open);
  };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        title="Notifications" aria-label={unread ? `Notifications (${unread} unread)` : "Notifications"} aria-expanded={open}
        onClick={toggle}
        style={{ ...buttonStyle, position: "relative", background: open ? "#F9FAFB" : "none" }}
        onMouseEnter={(e) => (e.currentTarget.style.background = "#F9FAFB")}
        onMouseLeave={(e) => { if (!open) e.currentTarget.style.background = "none"; }}
      >
        {/* Same bell as Agents' / Catalog's i-bell and ML Studio's bell icon. */}
        <Glyph name="pi-bell" size={16} />
        {unread > 0 && (
          <span style={{ position: "absolute", top: 7, right: 8, width: 7, height: 7, borderRadius: "50%", background: "#F97316", boxShadow: "0 0 0 2px #fff" }} />
        )}
      </button>

      {open && (
        <div style={{ position: "absolute", top: 42, right: 0, width: 340, background: "#fff", border: "1px solid #E5E7EB", borderRadius: 10, boxShadow: "0 12px 40px rgba(16,32,58,.18)", zIndex: 220, overflow: "hidden" }}>
          <div style={{ display: "flex", alignItems: "center", padding: "10px 14px", borderBottom: "1px solid #F3F4F6" }}>
            <span style={{ fontFamily: "var(--disp)", fontSize: 13.5, fontWeight: 600, color: "#1F2937" }}>Notifications</span>
          </div>
          <div style={{ padding: 4 }}>
            {log.length === 0 && (
              <div style={{ padding: "28px 14px", textAlign: "center", fontSize: 12.5, color: "#9CA3AF" }}>
                <i className="ti ti-bell-off" style={{ fontSize: 22, display: "block", marginBottom: 6 }} />
                No notifications yet
              </div>
            )}
            {shown.map((n) => {
              const k = KIND[n.kind];
              return (
                <div key={n.id} onMouseEnter={() => setHovered(n.id)} onMouseLeave={() => setHovered((h) => (h === n.id ? null : h))}
                  style={{ display: "flex", gap: 10, padding: "9px 10px", borderRadius: 6, alignItems: "flex-start", background: hovered === n.id ? "#F9FAFB" : "transparent" }}>
                  <span style={{ width: 26, height: 26, borderRadius: 7, background: k.bg, color: k.color, display: "grid", placeItems: "center", flexShrink: 0 }}>
                    <i className={`ti ${k.icon}`} style={{ fontSize: 15 }} />
                  </span>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 12.5, color: "#1F2937", lineHeight: 1.45, overflowWrap: "anywhere" }}>{n.message}</div>
                    <div style={{ fontSize: 11, color: "#9CA3AF", marginTop: 2 }}>{ago(n.at, now)}</div>
                  </div>
                  {/* Shown on hover, like the file tree's; kept in layout so the text does not jump. */}
                  <IconAction size="sm" icon={ACTION_ICON.delete} label="Delete this notification" danger
                    style={{ visibility: hovered === n.id ? "visible" : "hidden" }}
                    onClick={() => dispatch(removeNotification(n.id))} />
                </div>
              );
            })}
          </div>
          {log.length > 0 && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 10px", borderTop: "1px solid #F3F4F6" }}>
              <button type="button" aria-label="Previous notifications" disabled={current === 0} style={footerButton(false, current === 0)}
                onClick={() => setPage(current - 1)}>Prev</button>
              <button type="button" aria-label="Next notifications" disabled={current >= pages - 1} style={footerButton(false, current >= pages - 1)}
                onClick={() => setPage(current + 1)}>Next</button>
              <span style={{ flex: 1, textAlign: "center", fontSize: 11, color: "#9CA3AF" }}>
                {current * PAGE_SIZE + 1}–{current * PAGE_SIZE + shown.length} of {log.length}
              </span>
              <button type="button" aria-label="Delete all notifications" style={footerButton(true)}
                onClick={() => dispatch(clearNotifications())}>Delete all</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
