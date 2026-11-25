// Minimal inline line-icon set, single-stroke style, matching the mockup's
// `.ic` treatment (stroke:currentColor). One component keyed by name.
const PATHS: Record<string, JSX.Element> = {
  // The mock uses this single pulse line for both Dashboard and Model
  // Overview — the two "Overview" nav entries share one icon there.
  activity: <><path d="M3 12h4l3 8 4-16 3 8h4" /></>,
  "arrow-up": <><path d="M12 19V5M5 12l7-7 7 7" /></>,
  "arrow-down": <><path d="M12 5v14M19 12l-7 7-7-7" /></>,
  trend: <><path d="M5 12l5-5 4 4 5-7" /></>,
  dashboard: <><rect x="3" y="3" width="7" height="9" rx="1" /><rect x="14" y="3" width="7" height="5" rx="1" /><rect x="14" y="12" width="7" height="9" rx="1" /><rect x="3" y="16" width="7" height="5" rx="1" /></>,
  layers: <><polygon points="12 2 22 8.5 12 15 2 8.5 12 2" /><polyline points="2 15.5 12 22 22 15.5" /></>,
  box: <><path d="M21 8V16L12 21L3 16V8L12 3L21 8Z" /><polyline points="3 8 12 13 21 8" /><line x1="12" y1="13" x2="12" y2="21" /></>,
  wand: <><path d="M15 4V2M15 22v-2M8 9l-2-2M20 15l2 2M4 15l-2 2M20 9l2-2" /><path d="M14.5 9.5L4 20l-1-1L13.5 8.5z" /></>,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.66 3.58 3 8 3s8-1.34 8-3V5" /><path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3" /></>,
  branch: <><circle cx="6" cy="4" r="2.5" /><circle cx="6" cy="20" r="2.5" /><circle cx="18" cy="8" r="2.5" /><path d="M6 6.5v11M18 10.5c0 4-6 3-6 7" /></>,
  flask: <><path d="M9 2h6M10 2v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V2" /><line x1="7" y1="15" x2="17" y2="15" /></>,
  rocket: <><path d="M5 15c-2 1-3 5-3 5s4-1 5-3M9 12l3 3M15 4c3 0 5 2 5 5-.5 3-4 7-7 9l-4-4c2-3 6-6.5 9-7 .3-1.3.3-2.7-3-3z" /><circle cx="14.5" cy="9.5" r="1.3" /></>,
  plug: <><path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0V8ZM12 17v5" /></>,
  // Same bell as Agents' and UCS's i-bell, so Alerts reads as one icon across the platform.
  bell: <><path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6z" /><path d="M10 20a2 2 0 0 0 4 0" /></>,
  server: <><rect x="2" y="3" width="20" height="7" rx="2" /><rect x="2" y="14" width="20" height="7" rx="2" /><line x1="6" y1="6.5" x2="6.01" y2="6.5" /><line x1="6" y1="17.5" x2="6.01" y2="17.5" /></>,
  filter: <><path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3Z" /></>,
  "arrow-left": <><path d="M19 12H5M12 19l-7-7 7-7" /></>,
  list: <><line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" /></>,
  grid: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
  file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></>,
  check: <><polyline points="20 6 9 17 4 12" /></>,
  share: <><circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" /><line x1="8.6" y1="13.5" x2="15.4" y2="17.5" /><line x1="15.4" y1="6.5" x2="8.6" y2="10.5" /></>,
  message: <><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-4-.9L3 21l1.9-4a8.4 8.4 0 1 1 16.1-5.5z" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><polyline points="12 7 12 12 15 14" /></>,
  hash: <><line x1="4" y1="9" x2="20" y2="9" /><line x1="4" y1="15" x2="20" y2="15" /><line x1="10" y1="3" x2="8" y2="21" /><line x1="16" y1="3" x2="14" y2="21" /></>,
  type: <><polyline points="4 7 4 4 20 4 20 7" /><line x1="9" y1="20" x2="15" y2="20" /><line x1="12" y1="4" x2="12" y2="20" /></>,
  alert: <><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></>,
  maximize: <><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" /></>,
  key: <><path d="M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.8 7.8 5.5 5.5 0 0 1 7.8-7.8Zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3" /></>,
  register: <><path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" /><path d="M3 12l9 4.5 9-4.5" /></>,
  // ---- Action icons: exact paths from InaiEra_UI_Style_Spec.md §3, shared
  // with the UCS catalog and Agents sprites. One drawing per action. ----
  pencil: <><path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" /></>,
  trash: <><path d="M4 7h16" /><path d="M10 4h4a1 1 0 0 1 1 1v2H9V5a1 1 0 0 1 1-1Z" /><path d="M6 7v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7" /><path d="M10 11v6M14 11v6" /></>,
  play: <><path d="M6 4v16l14-8Z" /></>,
  // Test / test-connection: the plug from the Workspace app (Tabler plug-connected), one Test icon platform-wide.
  test: <><path d="M7 12l5 5l-1.5 1.5a3.536 3.536 0 1 1 -5 -5l1.5 -1.5z" /><path d="M17 12l-5 -5l1.5 -1.5a3.536 3.536 0 1 1 5 5l-1.5 1.5z" /><path d="M3 21l2.5 -2.5" /><path d="M18.5 5.5l2.5 -2.5" /><path d="M10 11l-2 2" /><path d="M13 14l-2 2" /></>,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" /></>,
  eye: <><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></>,
  plus: <><path d="M12 5v14M5 12h14" /></>,
  x: <><path d="M6 6l12 12M18 6 6 18" /></>,
  copy: <><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3" /></>,
  refresh: <><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16" /><path d="M3 21v-5h5" /><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8" /><path d="M21 3v5h-5" /></>,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  "chevron-down": <><path d="m6 9 6 6 6-6" /></>,
  "chevron-right": <><path d="m9 6 6 6-6 6" /></>,
  external: <><path d="M14 4h6v6" /><path d="M20 4 10 14" /><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" /></>,
  download: <><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></>,
  upload: <><path d="M12 15V3" /><path d="m7 8 5-5 5 5" /><path d="M5 21h14" /></>,
  more: <><circle cx="12" cy="5" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="12" cy="19" r="1" /></>,
};

export function Icon({ name, className }: { name: string; className?: string }) {
  return (
    <svg className={"ic" + (className ? " " + className : "")} viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      {PATHS[name] || null}
    </svg>
  );
}
