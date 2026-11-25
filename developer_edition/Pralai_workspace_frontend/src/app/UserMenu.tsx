/**
 * Header avatar + account menu for the standalone app: who you are signed in as, and sign out. Inside the Control Portal the
 * portal rail carries the profile instead (an older portal's `accountMenu` takes this place: AppHeader, api/apiBase.hostAccountMenu).
 */
import { useRef, useState } from "react";
import { useGetMeQuery } from "../features/workspace/api";
import { useClickOutside } from "../shared/hooks/useClickOutside";

const menuRow: React.CSSProperties = { display: "flex", alignItems: "center", gap: 7, padding: "7px 12px", cursor: "pointer", fontSize: 12.5 };
const hover = {
  onMouseEnter: (e: React.MouseEvent<HTMLDivElement>) => (e.currentTarget.style.background = "var(--ws-hover, #F8FAF9)"),
  onMouseLeave: (e: React.MouseEvent<HTMLDivElement>) => (e.currentTarget.style.background = "transparent"),
};

/** `onSignOut` clears the session cookie. */
export function UserMenu({ onSignOut }: { onSignOut: () => void }) {
  const { data: me } = useGetMeQuery();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, open, () => setOpen(false));

  const name = me?.display_name || me?.email || "";

  return (
    <div style={{ position: "relative" }} ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        title={name}
        aria-label="Account menu"
        style={{
          border: "none", cursor: "pointer", background: "transparent", padding: 0, color: "inherit", font: "inherit",
          display: "flex", alignItems: "center", gap: 8,
        }}
      >
        <span style={{ width: 32, height: 32, borderRadius: "50%", background: "#0a7d5a", color: "#fff", display: "grid", placeItems: "center", fontSize: 14, fontWeight: 600, flexShrink: 0 }}>
          {(name || "?")[0].toUpperCase()}
        </span>
        <span style={{ fontSize: 14, fontWeight: 500, whiteSpace: "nowrap" }}>Hi {name.split(" ")[0]}</span>
      </button>
      {open && (
        <div
          style={{
            position: "absolute", top: 38, right: 0, minWidth: 250,
            background: "var(--ws-surface, #fff)", border: "1px solid var(--ws-line, #E3E8E2)",
            borderRadius: 10, boxShadow: "0 12px 40px rgba(11,59,46,.18)",
            zIndex: 210, overflow: "hidden", padding: "4px 0",
          }}
        >
          <div style={{ padding: "10px 12px 8px" }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ws-text, #14201B)" }}>{me?.display_name || "Signed in"}</div>
            <div style={{ fontSize: 11.5, color: "var(--ws-muted, #5b6d62)", overflow: "hidden", textOverflow: "ellipsis" }}>{me?.email}</div>
          </div>
          <div style={{ height: 0.5, background: "var(--ws-line-fill, #e3e8e2)", margin: "4px 0" }} />
          <div onClick={() => { setOpen(false); onSignOut(); }} style={{ ...menuRow, color: "var(--ws-bad, #b23b3b)" }} {...hover}>
            <i className="ti ti-logout" style={{ fontSize: 13 }} />Sign out
          </div>
        </div>
      )}
    </div>
  );
}
