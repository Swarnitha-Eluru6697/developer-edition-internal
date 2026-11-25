/**
 * Developer Edition account menu: the same avatar + "Hi <name>" button and menu in every product (Data Connect,
 * Workspace, UCS, Agents, ML Studio), matching the dashboard's menu at localhost:8080.
 *
 * Source of truth: devedition/shared/DeveloperUserMenu.tsx. Each product keeps an identical copy (separate repos and
 * builds); after changing it, copy it again with devedition/shared/sync.sh.
 *
 * Who is signed in comes from the Developer Control Plane shell, which owns the session (gateway html/auth.js):
 *   app → shell   { type: "inaiera:hello" }      on mount, when running inside the shell
 *   shell → app   { type: "inaiera:user", user }  name, email, company, role of the signed-in account
 *   app → shell   { type: "inaiera:sign-out" }   Sign out: the shell ends the session and closes every app
 *   shell → app   { type: "inaiera:theme", theme }  "light" | "dark": the shell's theme toggle; the product sets
 *                 <html data-theme> and its dark styles (pralai-dark.css + the product's own overrides) apply.
 * The first theme also arrives in the frame URL (?de_theme=dark) so the product paints dark from the start.
 * Opened on its own port, the product follows the OS light/dark setting.
 * Opened on its own port (not inside the shell), Sign out goes to the shell with ?signout=1, which signs out there.
 * The product's own /me name is only the fallback until the shell answers.
 */
import { useEffect, useRef, useState } from "react";

type ShellUser = { name?: string; email?: string; company?: string | null; role?: string | null };

const SHELL_PORT = "8080";
const shellOrigin = () => `${window.location.protocol}//${window.location.hostname}:${SHELL_PORT}`;
const inShell = () => window.parent !== window;
/**
 * True when this product is open inside the Developer Edition dashboard (the shell on :8080). The shell's left rail then
 * carries notifications and the account menu (sign out), so the product's top bar leaves out its own bell and avatar.
 */
export function inDeveloperShell(): boolean {
  if (!inShell()) return false;
  const top = window.location.ancestorOrigins?.[0] || (document.referrer ? new URL(document.referrer).origin : "");
  return top === shellOrigin();
}

// ── Theme (module level: runs as soon as the product imports this file, before any menu renders) ──
export type DeTheme = "light" | "dark";
const themeListeners = new Set<(t: DeTheme) => void>();
function initialTheme(): DeTheme {
  try {
    const q = new URLSearchParams(window.location.search).get("de_theme");
    if (q === "dark" || q === "light") { sessionStorage.setItem("de-theme", q); return q; }
    const kept = sessionStorage.getItem("de-theme");
    if (kept === "dark" || kept === "light") return kept;
  } catch { /* storage blocked */ }
  if (inShell()) return "light";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
export function applyTheme(t: DeTheme) {
  document.documentElement.setAttribute("data-theme", t);
  document.documentElement.style.colorScheme = t;
  try { sessionStorage.setItem("de-theme", t); } catch { /* storage blocked */ }
  themeListeners.forEach((fn) => fn(t));
}
export const currentTheme = (): DeTheme =>
  document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
if (typeof window !== "undefined") {
  applyTheme(initialTheme());
  window.addEventListener("message", (e: MessageEvent) => {
    if (e.origin !== shellOrigin() || e.data?.type !== "inaiera:theme") return;
    if (e.data.theme === "dark" || e.data.theme === "light") applyTheme(e.data.theme);
  });
  if (inShell()) window.parent.postMessage({ type: "inaiera:hello" }, shellOrigin());
}
/** The current theme, re-rendering when the shell switches it. */
export function useDeTheme(): DeTheme {
  const [t, setT] = useState<DeTheme>(currentTheme());
  useEffect(() => { themeListeners.add(setT); return () => { themeListeners.delete(setT); }; }, []);
  return t;
}

/** Signs out of Developer Edition (every product), not just this one. */
export function signOutOfDeveloperEdition() {
  if (inShell()) window.parent.postMessage({ type: "inaiera:sign-out" }, shellOrigin());
  else window.location.href = `${shellOrigin()}/?signout=1`;
}

function useShellUser(): ShellUser | null {
  const [user, setUser] = useState<ShellUser | null>(null);
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== shellOrigin() || e.data?.type !== "inaiera:user") return;
      setUser(e.data.user || null);
    };
    window.addEventListener("message", onMessage);
    if (inShell()) window.parent.postMessage({ type: "inaiera:hello" }, shellOrigin());
    return () => window.removeEventListener("message", onMessage);
  }, []);
  return user;
}

const ORANGE = "#f97316";
const rowStyle: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 8, padding: "9px 16px", fontSize: 13.5, color: "#374151",
};
const svgProps = {
  width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
  strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true,
};

export function DeveloperUserMenu({ name: fallbackName = "", email: fallbackEmail = "" }: { name?: string; email?: string }) {
  const shell = useShellUser();
  const dark = useDeTheme() === "dark";
  const C = dark
    ? { bg: "#1A1E22", line: "#2C3238", text: "#E8EAED", sub: "#A3ABB3", icon: "#7C858D", hover: "#22272C", danger: "#FCA5A5" }
    : { bg: "#fff", line: "#e5e7eb", text: "#111827", sub: "#6b7280", icon: "#9ca3af", hover: "#f9fafb", danger: "#b91c1c" };
  const name = shell?.name || fallbackName || "Developer";
  const email = shell?.email || fallbackEmail;
  const edition = `${shell?.company || "Developer Edition"} / ${shell?.role || "Developer"}`;

  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); buttonRef.current?.focus(); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        ref={buttonRef} type="button" onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu" aria-expanded={open} aria-label={`Signed in as ${name}`} title={name}
        style={{
          display: "flex", alignItems: "center", gap: 8, border: 0, background: "transparent", padding: 0,
          cursor: "pointer", color: "inherit", font: "inherit",
        }}
      >
        <span style={{
          width: 32, height: 32, borderRadius: "50%", background: ORANGE, color: "#fff", display: "grid",
          placeItems: "center", fontSize: 14, fontWeight: 600, flexShrink: 0,
        }}>{(name[0] || "D").toUpperCase()}</span>
        <span style={{ fontSize: 14, fontWeight: 500, whiteSpace: "nowrap" }}>Hi {name.split(" ")[0]}</span>
        <svg {...svgProps} style={{ color: C.icon }}><path d="m6 9 6 6 6-6" /></svg>
      </button>
      {open && (
        <div role="menu" style={{
          position: "absolute", right: 0, top: "calc(100% + 8px)", width: 260, background: C.bg,
          border: `1px solid ${C.line}`, borderRadius: 12, boxShadow: "0 12px 32px rgba(17,24,39,.14)",
          padding: "4px 0", zIndex: 1000, textAlign: "left",
        }}>
          <div style={{ padding: "10px 16px", borderBottom: `1px solid ${C.line}` }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: C.text }}>{name}</div>
            {email && <div style={{ fontSize: 12, color: C.sub, marginTop: 2, overflowWrap: "anywhere" }}>{email}</div>}
          </div>
          <div style={{ ...rowStyle, color: C.text }}>
            <svg {...svgProps} style={{ color: C.icon, flexShrink: 0 }}>
              <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />
              <path d="m9 12 2 2 4-4" />
            </svg>
            <span>{edition}</span>
          </div>
          <div style={{ height: 1, background: C.line, margin: "4px 0" }} />
          <button
            type="button" role="menuitem" onClick={() => { setOpen(false); signOutOfDeveloperEdition(); }}
            style={{ ...rowStyle, width: "100%", border: 0, background: "transparent", font: "inherit", fontSize: 13.5, cursor: "pointer", color: C.danger }}
            onMouseEnter={(e) => (e.currentTarget.style.background = C.hover)}
            onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
          >
            <svg {...svgProps}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line x1="21" x2="9" y1="12" y2="12" /></svg>
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
