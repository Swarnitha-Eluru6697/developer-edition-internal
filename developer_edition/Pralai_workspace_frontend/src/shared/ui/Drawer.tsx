/**
 * Right-side slide-over drawer, plus the labelled field its forms use (the
 * matching input/button styles live in ./drawerStyles, so this module only
 * exports components and stays Fast-Refresh friendly).
 *
 * Shared by the FlinkSQL job and dbt project registration forms so the two
 * behave identically rather than drifting apart.
 *
 * Closing: ×, backdrop, Escape, or whatever the footer wires up. All of them
 * are suppressed while `busy` is set — dismissing mid-request would hide a call
 * that's still running.
 */
import { useEffect } from "react";
import { hiddenScrollbar } from "./drawerStyles";
import { Glyph } from "./Glyph";

interface DrawerProps {
  title: string;
  /** Tabler icon class ("ti-git-fork") or a platform glyph ("pi-pencil"). */
  icon: string;
  onClose: () => void;
  /** True while a submit is in flight — disables Escape / backdrop dismissal. */
  busy?: boolean;
  /** Pinned action row at the bottom; scrolls independently of the body. */
  footer: React.ReactNode;
  children: React.ReactNode;
  width?: number;
  /** Draw no scrollbar on the body (it still scrolls). */
  hideScrollbar?: boolean;
}

export function Drawer({ title, icon, onClose, busy, footer, children, width = 420, hideScrollbar }: DrawerProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  return (
    <>
      <div
        onClick={() => !busy && onClose()}
        style={{ position: "fixed", inset: 0, background: "rgba(15,17,23,0.28)", zIndex: 1200 }}
      />
      <div
        role="dialog"
        aria-label={title}
        style={{
          position: "fixed", top: 0, right: 0, bottom: 0, width, maxWidth: "92vw",
          background: "var(--ws-surface, #fff)", zIndex: 1201, display: "flex", flexDirection: "column",
          boxShadow: "-8px 0 30px rgba(0,0,0,0.16)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "13px 16px", borderBottom: "0.5px solid var(--ws-line, #E3E8E2)", flexShrink: 0 }}>
          <Glyph name={icon} size={16} style={{ color: "var(--ws-accent, #0A7D5A)" }} />
          <span style={{ fontSize: 14, fontWeight: 600 }}>{title}</span>
          <button
            onClick={onClose}
            title="Close (Esc)"
            style={{ marginLeft: "auto", background: "none", border: "none", cursor: "pointer", color: "var(--ws-subtle, #73827a)", fontSize: 18, lineHeight: 1, padding: 2 }}
          >
            <Glyph name="pi-x" size={16} />
          </button>
        </div>

        <div data-testid="drawer-body" style={{ flex: 1, overflow: "auto", padding: "14px 16px", ...(hideScrollbar ? hiddenScrollbar : {}) }}>{children}</div>

        <div style={{ display: "flex", gap: 8, padding: "12px 16px", borderTop: "0.5px solid var(--ws-line, #E3E8E2)", flexShrink: 0 }}>
          {footer}
        </div>
      </div>
    </>
  );
}

/**
 * One labelled row. Drawer fields stack, so each carries a real label rather
 * than relying on a placeholder that disappears as soon as you type.
 */
export function Field({ label, hint, required, children }: {
  label: string; hint?: string; required?: boolean; children: React.ReactNode;
}) {
  return (
    <div style={{ marginBottom: 12 }}>
      <label style={{ display: "block", fontSize: 11.5, fontWeight: 600, color: "var(--ws-text-2, #2E3D36)", marginBottom: 4 }}>
        {label}{required && <span style={{ color: "var(--ws-bad, #b23b3b)", marginLeft: 2 }}>*</span>}
      </label>
      {children}
      {hint && <div style={{ fontSize: 11, color: "var(--ws-subtle, #73827a)", marginTop: 3, lineHeight: 1.45 }}>{hint}</div>}
    </div>
  );
}
