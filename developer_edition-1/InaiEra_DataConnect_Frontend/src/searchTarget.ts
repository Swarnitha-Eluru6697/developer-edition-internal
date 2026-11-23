/*
 * What the top bar's search hands to a page when you pick a result (components/HeaderSearch.tsx → DataConnectApp.tsx → the page).
 *
 * The shell switches to `page`; that page reads `focus` once it is mounted, lands on the item (opens its details, or filters its list to
 * it) and calls `clearFocus()` so a later visit to the page isn't hijacked by an old pick.
 */
import type { PageId } from "./DataConnectApp";

export type SearchTargetKind = "pipeline" | "connection" | "schema" | "policy" | "transform" | "template";

export interface SearchTarget {
  kind: SearchTargetKind;
  page: PageId;
  id: string;
  name: string;
  /** A connection's source type — the Connections page lists connections per type, so it needs to know which list to open. */
  sourceType?: string;
}

/** The two props a page takes to land on a picked search result. */
export interface PageFocusProps {
  focus?: SearchTarget | null;
  clearFocus?: () => void;
}
