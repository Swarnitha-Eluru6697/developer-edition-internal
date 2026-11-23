import React from "react";

/**
 * An icon-only refresh, the same button on every page that has one. By default it does what the browser's own refresh does: it reloads
 * the page the moment it is clicked, with no animation of its own (the open page and the chosen connector are remembered across
 * a reload, so you land back where you were). A page that only wants fresh data passes `reload` (e.g. `refetch`) and, while that runs,
 * `busy` — the icon turns only while a request is really in flight. `reload` is also how the tests stand in for the page reload.
 */
export default function RefreshButton({ label = "Refresh page", reload = () => window.location.reload(), busy = false }: {
  label?: string; reload?: () => void; busy?: boolean;
}) {
  return (
    <button type="button" className="btn-s icon-btn" aria-label={label} title={label} aria-busy={busy} onClick={() => reload()}>
      <svg className={busy ? "spin-icon" : undefined} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M21 12a9 9 0 1 1-2.64-6.36" />
        <path d="M21 3v6h-6" />
      </svg>
    </button>
  );
}
