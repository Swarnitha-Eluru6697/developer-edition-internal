/**
 * Icons that must look identical across the InAiEra apps are drawn here with
 * the platform's shared paths (InaiEra_UI_Style_Spec.md §3 — the same drawings
 * as Agents' / Catalog's i-* sprite and ML Studio's Icons). Any other name is a
 * Tabler class and renders through the icon font as before.
 */
import type { CSSProperties, ReactElement } from "react";

const PLATFORM: Record<string, ReactElement> = {
  "pi-cog": <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" /></>,
  "pi-pencil": <><path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" /></>,
  "pi-trash": <><path d="M4 7h16" /><path d="M10 4h4a1 1 0 0 1 1 1v2H9V5a1 1 0 0 1 1-1Z" /><path d="M6 7v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7" /><path d="M10 11v6M14 11v6" /></>,
  "pi-eye": <><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></>,
  "pi-x": <><path d="M6 6l12 12M18 6 6 18" /></>,
  "pi-refresh": <><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16" /><path d="M3 21v-5h5" /><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8" /><path d="M21 3v5h-5" /></>,
  // The browser's reload icon: one circular arrow, arrowhead at the top right (Tabler "reload"). Used for every Refresh button;
  // "pi-refresh" above (two arrows) stays for "sync".
  "pi-reload": <><path d="M19.933 13.041a8 8 0 1 1 -9.925 -8.788c3.899 -1 7.935 1.007 9.425 4.747" /><path d="M20 4v5h-5" /></>,
  "pi-download": <><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></>,
  "pi-copy": <><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3" /></>,
  "pi-bell": <><path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6z" /><path d="M10 20a2 2 0 0 0 4 0" /></>,
  // Test / test-connection (Tabler plug-connected) — the other apps' i-test.
  "pi-test": <><path d="M7 12l5 5l-1.5 1.5a3.536 3.536 0 1 1 -5 -5l1.5 -1.5z" /><path d="M17 12l-5 -5l1.5 -1.5a3.536 3.536 0 1 1 5 5l-1.5 1.5z" /><path d="M3 21l2.5 -2.5" /><path d="M18.5 5.5l2.5 -2.5" /><path d="M10 11l-2 2" /><path d="M13 14l-2 2" /></>,
};

/** `name`: a platform icon ("pi-pencil") or a Tabler class ("ti-folder"). `size` in px. */
export function Glyph({ name, size = 16, style }: { name: string; size?: number; style?: CSSProperties }) {
  const paths = PLATFORM[name];
  if (!paths) return <i className={`ti ${name}`} style={{ fontSize: size, lineHeight: 1, ...style }} />;
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flexShrink: 0, display: "block", ...style }}>
      {paths}
    </svg>
  );
}
