/*
 * The topbar bell: an unread badge, and a dropdown of what needs attention —
 * fired alerts, versions awaiting promotion, recent training runs (see
 * useNotifications for where each comes from). Same look and dismissal rules as
 * other topbar menus: closes on an outside click or Escape.
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { usePermissions } from "../context/PermissionsContext";
import { Icon } from "./Icons";
import { useNotifications, type AppNotification } from "./useNotifications";

function relTime(iso: string | null): string {
  if (!iso) return "";
  const secs = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return days < 30 ? `${days}d ago` : new Date(iso).toLocaleDateString();
}

export default function NotificationsBell() {
  const { canReach } = usePermissions();
  const navigate = useNavigate();
  const { items, unread, error, loading, refresh, markAllRead, markOpened, isUnread } = useNotifications();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    refresh();
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setOpen(false); btnRef.current?.focus(); }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
    // refresh is deliberately not a dependency: it changes with the permissions, which
    // the hook already refetches on — this effect is only about opening.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Opening a notification removes it from the list.
  const go = (n: AppNotification) => { markOpened(n.id); setOpen(false); navigate(n.to); };
  const badge = unread > 99 ? "99+" : String(unread);
  const tip = unread ? `Notifications · ${unread} unread` : "Notifications";
  // The footer points at the main page for what's listed, as far as the user can go.
  const footer = canReach("/alerts") ? { to: "/alerts", label: "View all alerts" }
    : canReach("/pipelines") ? { to: "/pipelines", label: "View all pipeline runs" } : null;

  return (
    <div className="notif" ref={rootRef}>
      <button ref={btnRef} type="button" className="topbar-icon-btn has-tip" aria-label={tip} data-tip={tip}
        aria-haspopup="true" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon name="bell" />
        {unread > 0 && <span className="notif-badge" aria-hidden="true">{badge}</span>}
      </button>
      {open && (
        <div className="notif-menu" role="group" aria-label="Notifications">
          <div className="notif-hd">
            <span className="notif-hd-title">Notifications</span>
            <button type="button" className="notif-mark" onClick={markAllRead} disabled={!unread}>Mark all as read</button>
          </div>
          <div className="notif-list">
            {error ? (
              <div className="notif-empty">
                Couldn't load notifications
                <button type="button" className="notif-mark" onClick={refresh} disabled={loading}>{loading ? "Retrying…" : "Retry"}</button>
              </div>
            ) : items === null ? (
              <div className="notif-empty">Loading…</div>
            ) : items.length === 0 ? (
              <div className="notif-empty">You're all caught up</div>
            ) : (
              items.map((n) => {
                const fresh = isUnread(n);
                return (
                  <button key={n.id} type="button" className={"notif-item" + (fresh ? " unread" : "")} onClick={() => go(n)}
                    aria-label={`${fresh ? "Unread: " : ""}${n.title}. ${n.detail}`}>
                    <span className={`notif-dot k-${n.kind}`} aria-hidden="true" />
                    <span className="notif-item-body">
                      <span className="notif-item-title">{n.title}</span>
                      <span className="notif-item-detail">{n.detail}</span>
                    </span>
                    <span className="notif-item-time">{relTime(n.time)}</span>
                  </button>
                );
              })
            )}
          </div>
          {footer && (
            <button type="button" className="notif-foot" onClick={() => { setOpen(false); navigate(footer.to); }}>
              {footer.label} <Icon name="chevron-right" />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
