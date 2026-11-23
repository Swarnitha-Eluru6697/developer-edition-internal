/**
 * Header avatar + account menu for the standalone app: who you are signed in as, and sign out. Inside the Control Portal the
 * portal rail carries the profile instead (an older portal's `accountMenu` takes this place: AppHeader, api/apiBase.hostAccountMenu).
 */
import { useRef, useState } from "react";
import { useGetMeQuery } from "../features/workspace/api";
import { useClickOutside } from "../shared/hooks/useClickOutside";

const menuRow: React.CSSProperties = { display: "flex", alignItems: "center", gap: 7, padding: "7px 12px", cursor: "pointer", fontSize: 12.5 };
const hover = {
  onMouseEnter: (e: React.MouseEvent<HTMLDivElement>) => (e.currentTarget.style.background = "#F9FAFB"),
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
        <span style={{ width: 32, height: 32, borderRadius: "50%", background: "#f97316", color: "#fff", display: "grid", placeItems: "center", fontSize: 14, fontWeight: 600, flexShrink: 0 }}>
          {(name || "?")[0].toUpperCase()}
        </span>
        <span style={{ fontSize: 14, fontWeight: 500, whiteSpace: "nowrap" }}>Hi {name.split(" ")[0]}</span>
      </button>
      {open && (
        <div
          style={{
            position: "absolute", top: 38, right: 0, minWidth: 250,
            background: "#fff", border: "1px solid #E5E7EB",
            borderRadius: 10, boxShadow: "0 12px 40px rgba(16,32,58,.18)",
            zIndex: 210, overflow: "hidden", padding: "4px 0",
          }}
        >
          <div style={{ padding: "10px 12px 8px" }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#1F2937" }}>{me?.display_name || "Signed in"}</div>
            <div style={{ fontSize: 11.5, color: "#6b7280", overflow: "hidden", textOverflow: "ellipsis" }}>{me?.email}</div>
          </div>
          <div style={{ height: 0.5, background: "#e5e7eb", margin: "4px 0" }} />
          <div onClick={() => { setOpen(false); onSignOut(); }} style={{ ...menuRow, color: "#b91c1c" }} {...hover}>
            <i className="ti ti-logout" style={{ fontSize: 13 }} />Sign out
          </div>
        </div>
      )}
    </div>
  );
}
