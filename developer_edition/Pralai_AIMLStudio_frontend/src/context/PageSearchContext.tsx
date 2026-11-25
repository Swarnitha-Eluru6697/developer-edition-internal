import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

/**
 * The top-bar search, shared with the page on screen.
 *
 * A page with lists calls usePageSearch("Search pipelines…"): the top-bar box then
 * filters that page as you type, with that placeholder. A page that registers
 * nothing (Dashboard, detail pages) keeps the global behaviour — Enter jumps to the
 * matching model, base model or dataset. The term resets when you change page,
 * except to a `?q=` the URL carries (the global search lands that way).
 */
interface PageSearch {
  query: string;
  setQuery: (q: string) => void;
  placeholder: string | null;
  setPlaceholder: (p: string | null) => void;
}

const Ctx = createContext<PageSearch>({ query: "", setQuery: () => {}, placeholder: null, setPlaceholder: () => {} });

export function PageSearchProvider({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation();
  const [query, setQuery] = useState(() => new URLSearchParams(search).get("q") ?? "");
  const [placeholder, setPlaceholder] = useState<string | null>(null);
  useEffect(() => {
    setQuery(new URLSearchParams(window.location.search).get("q") ?? "");
  }, [pathname]);
  return <Ctx.Provider value={{ query, setQuery, placeholder, setPlaceholder }}>{children}</Ctx.Provider>;
}

/** The top bar's own access: the term, and the current page's placeholder (null = global search). */
export const usePageSearchState = () => useContext(Ctx);

/** Makes the top-bar search filter this page; returns the current term. */
export function usePageSearch(placeholder: string): [string, (q: string) => void] {
  const { query, setQuery, setPlaceholder } = useContext(Ctx);
  useEffect(() => {
    setPlaceholder(placeholder);
    return () => setPlaceholder(null);
  }, [placeholder, setPlaceholder]);
  return [query, setQuery];
}

/** Case-insensitive "every word appears somewhere in these fields". */
export function matchesQuery(query: string, ...fields: (string | number | null | undefined)[]): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = fields.filter((f) => f !== null && f !== undefined).join(" ").toLowerCase();
  return words.every((w) => hay.includes(w));
}

/** matchesQuery over every top-level text or number field of a record. */
export function matchesRecord(query: string, record: object): boolean {
  if (!query.trim()) return true;
  return matchesQuery(query, ...Object.values(record).filter((v): v is string | number => typeof v === "string" || typeof v === "number"));
}
