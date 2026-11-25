/*
 * Sidebar icons (the connector-monitoring design's outline set), as real React SVG elements. Each one is a plain function returning JSX, so
 * React builds the nodes itself — there is no HTML string anywhere to parse or inject.
 */
import type React from "react";
import type { PageId } from "../DataConnectApp";

const Frame = ({ children }: { children: React.ReactNode }) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
);

const SHAPES: Record<PageId, () => React.JSX.Element> = {
  dash: () => (
    <Frame>
      <rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" />
    </Frame>
  ),
  mon: () => <Frame><path d="M3 12h4l3-8 4 16 3-8h4" /></Frame>,
  // Alerts: a bell (Tabler `bell`), the same glyph the Workspace's Alerts page uses.
  alrt: () => <Frame><path d="M10 5a2 2 0 1 1 4 0a7 7 0 0 1 4 6v3a4 4 0 0 0 2 3h-16a4 4 0 0 0 2 -3v-3a7 7 0 0 1 4 -6" /><path d="M9 17v1a3 3 0 0 0 6 0v-1" /></Frame>,
  conn: () => <Frame><path d="M9 7H6a5 5 0 0 0 0 10h3M15 7h3a5 5 0 0 1 0 10h-3M8 12h8" /></Frame>,
  pip: () => <Frame><circle cx="5" cy="6" r="2" /><circle cx="19" cy="18" r="2" /><path d="M7 6h6a4 4 0 0 1 4 4v6" /></Frame>,
  sch: () => <Frame><path d="M4 6h16M4 12h16M4 18h10" /></Frame>,
  qual: () => <Frame><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z" /><path d="M9 12l2 2 4-4" /></Frame>,
  lin: () => <Frame><circle cx="6" cy="12" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="18" cy="18" r="2" /><path d="M8 12h4l4-6M12 12l4 6" /></Frame>,
  trn: () => <Frame><path d="M4 7h11l-3-3M20 17H9l3 3" /></Frame>,
  cat: () => <Frame><rect x="4" y="4" width="16" height="16" rx="2" /><path d="M4 10h16M10 10v10" /></Frame>,
};

/** The sidebar icon for a page. */
export default function NavIcon({ id }: { id: PageId }) {
  const Shape = SHAPES[id];
  return <Shape />;
}
