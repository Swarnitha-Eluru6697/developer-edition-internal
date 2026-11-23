/*
 * The top bar's global search — one box for everything in DataConnect: pipelines, connections, schemas, quality policies, transform policies,
 * connector templates and the pages themselves. Same box and behaviour as Workspace's header search (app/HeaderSearch.tsx): 38px, gray fill,
 * 1px border, a `/` hint that also focuses it, a grouped results list, Up/Down to move, Enter to open, Esc to close.
 *
 * It searches what the app already loads (the same cached queries the pages use), so it adds no endpoint of its own. A list is only fetched
 * once the box has been focused, and only for pages the caller's permissions open — a search never asks for something the caller can't see.
 * Picking a result goes to its page and lands on the item (DataConnectApp.openTarget → the page's `focus`, see searchTarget.ts).
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import NavIcon from "./NavIcons";
import { IconSearch } from "./ui";
import {
  useGetConnectionsQuery, useListConnectionConfigSchemasQuery, useGetPipelinesQuery, useGetQualityPoliciesQuery, useGetSchemasQuery, useGetTransformsQuery, useListTemplatesQuery,
} from "../store/apiSlice";
import type { PageId } from "../DataConnectApp";
import type { SearchTarget } from "../searchTarget";

const PER_GROUP = 5;
const TOTAL = 24;

interface Entry {
  key: string;
  group: string;
  label: string;
  detail?: string;
  icon: PageId;
  /** Everything a query may match, lower-cased. */
  haystack: string;
  /** Goes to a page. */
  page?: PageId;
  /** Or lands on an item. */
  target?: SearchTarget;
}

export interface SearchPage { id: PageId; title: string }

const text = (...parts: unknown[]) => parts.filter((p) => typeof p === "string" || typeof p === "number").join(" ").toLowerCase();

/** Lower is better: starts with the query, then contains it in the name, then only elsewhere. */
function rank(entry: Entry, q: string): number {
  const label = entry.label.toLowerCase();
  return label.startsWith(q) ? 0 : label.includes(q) ? 1 : 2;
}

export default function HeaderSearch({ pages, canOpen, onGo, onOpen }: {
  /** The pages the caller can see, in sidebar order. */
  pages: SearchPage[];
  /** Whether the caller can open a page (decides which lists are searched). */
  canOpen: (id: PageId) => boolean;
  onGo: (id: PageId) => void;
  onOpen: (target: SearchTarget) => void;
}) {
  const [query, setQuery] = useState("");
  const [armed, setArmed] = useState(false);           // set on first focus: nothing is fetched for a box nobody uses
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const skip = (id: PageId) => !armed || !canOpen(id);
  const { data: pipelines } = useGetPipelinesQuery(undefined, { skip: skip("pip") });
  const { data: connections } = useGetConnectionsQuery(undefined, { skip: skip("conn") });
  const { data: connectionTypes } = useListConnectionConfigSchemasQuery(undefined, { skip: skip("conn") });
  const { data: schemas } = useGetSchemasQuery(undefined, { skip: skip("sch") });
  const { data: policies } = useGetQualityPoliciesQuery(undefined, { skip: skip("qual") });
  const { data: transforms } = useGetTransformsQuery(undefined, { skip: skip("trn") });
  const { data: templates } = useListTemplatesQuery(undefined, { skip: skip("cat") });

  const entries = useMemo<Entry[]>(() => {
    const out: Entry[] = pages.map((p) => ({
      key: `page:${p.id}`, group: "Pages", label: p.title, detail: "Open page", icon: p.id, haystack: text(p.title, "page"), page: p.id,
    }));
    (pipelines ?? []).forEach((p) => out.push({
      key: `pipeline:${p.id}`, group: "Pipelines", label: p.name, detail: [p.status, p.topic_name].filter(Boolean).join(" · "), icon: "pip",
      haystack: text(p.name, p.topic_name, p.connector_name, p.entity, p.domain, p.source_system, p.status),
      target: { kind: "pipeline", page: "pip", id: p.id, name: p.name },
    }));
    // The types themselves (couchbase, redis, s3, ...): picking one opens that type's list, where a new connection is created.
    const inUse = new Map<string, number>();
    (connections ?? []).forEach((c) => inUse.set(c.source_type, (inUse.get(c.source_type) ?? 0) + 1));
    (connectionTypes ?? []).forEach((t) => out.push({
      key: `connection-type:${t.source_type}`, group: "Connection types", label: t.source_type,
      detail: `${inUse.get(t.source_type) ?? 0} ${(inUse.get(t.source_type) ?? 0) === 1 ? "connection" : "connections"}`, icon: "conn", haystack: text(t.source_type, "connection type"),
      target: { kind: "connection", page: "conn", id: "", name: "", sourceType: t.source_type },
    }));
    (connections ?? []).forEach((c) => {
      const cfg = (c.connection_config ?? {}) as Record<string, unknown>;
      out.push({
        key: `connection:${c.id}`, group: "Connections", label: c.name, detail: c.source_type, icon: "conn",
        haystack: text(c.name, c.source_type, cfg.hostname, cfg.host, cfg.database),
        target: { kind: "connection", page: "conn", id: c.id, name: c.name, sourceType: c.source_type },
      });
    });
    (schemas ?? []).forEach((s) => {
      const name = `${s.domain}.${s.source_system}.${s.entity}`;
      out.push({
        key: `schema:${s.id}`, group: "Schemas", label: name, detail: s.version, icon: "sch", haystack: text(name, s.version, s.id),
        target: { kind: "schema", page: "sch", id: s.id, name },
      });
    });
    (policies ?? []).forEach((q) => out.push({
      key: `policy:${q.id}`, group: "Quality Checks", label: q.name, detail: `${(q.rules ?? []).length} rules`, icon: "qual",
      haystack: text(q.name), target: { kind: "policy", page: "qual", id: q.id, name: q.name },
    }));
    (transforms ?? []).forEach((t) => out.push({
      key: `transform:${t.id}`, group: "SMT chains", label: t.name, detail: `${(t.transforms ?? []).length} SMTs`, icon: "trn",
      haystack: text(t.name), target: { kind: "transform", page: "trn", id: t.id, name: t.name },
    }));
    (templates ?? []).forEach((t) => out.push({
      key: `template:${t.id}`, group: "Templates", label: t.name, detail: [t.connector_type, t.category].filter(Boolean).join(" · "), icon: "cat",
      haystack: text(t.name, t.connector_type, t.category), target: { kind: "template", page: "cat", id: t.id, name: t.name },
    }));
    return out;
  }, [pages, pipelines, connections, connectionTypes, schemas, policies, transforms, templates]);

  const q = query.trim().toLowerCase();
  const results = useMemo<Entry[]>(() => {
    if (!q) return [];
    const tokens = q.split(/\s+/);
    const matched = entries.filter((e) => tokens.every((t) => e.haystack.includes(t)));
    const groups = new Map<string, Entry[]>();
    for (const e of matched) {
      const list = groups.get(e.group) ?? [];
      list.push(e);
      groups.set(e.group, list);
    }
    const out: Entry[] = [];
    for (const list of groups.values()) out.push(...list.sort((a, b) => rank(a, q) - rank(b, q)).slice(0, PER_GROUP));
    return out.slice(0, TOTAL);
  }, [entries, q]);

  useEffect(() => { setHighlight(0); }, [q]);

  // "/" focuses the search from anywhere that isn't a text field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable)) return;
      e.preventDefault();
      inputRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const choose = (e: Entry) => {
    setOpen(false);
    setQuery("");
    inputRef.current?.blur();
    if (e.target) onOpen(e.target);
    else if (e.page) onGo(e.page);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") { setOpen(false); inputRef.current?.blur(); return; }
    if (!results.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setHighlight((h) => (h + 1) % results.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setHighlight((h) => (h - 1 + results.length) % results.length); }
    else if (e.key === "Enter") { e.preventDefault(); const picked = results[highlight]; if (picked) choose(picked); }
  };

  const showList = open && q !== "";
  let lastGroup = "";
  return (
    <div className="hs" ref={rootRef}>
      <div className="hs-box">
        <IconSearch />
        <input
          ref={inputRef} className="hs-input" type="text" role="combobox" autoComplete="off" spellCheck={false}
          aria-label="Search DataConnect" aria-expanded={showList} aria-controls="hs-results" aria-autocomplete="list"
          aria-activedescendant={showList && results[highlight] ? `hs-opt-${highlight}` : undefined}
          placeholder="Search pipelines, connections, schemas, quality checks…" value={query}
          onFocus={() => { setArmed(true); setOpen(true); }}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onKeyDown={onKeyDown}
        />
        <kbd className="hs-kbd" aria-hidden="true">/</kbd>
      </div>
      {showList && (
        <div className="hs-pop" id="hs-results" role="listbox" aria-label="Search results">
          {results.length === 0 && <div className="hs-none" role="status">No matches for “{query.trim()}”</div>}
          {results.map((r, i) => {
            const header = r.group !== lastGroup;
            lastGroup = r.group;
            return (
              <React.Fragment key={r.key}>
                {header && <div className="hs-grp" role="presentation">{r.group}</div>}
                <div
                  id={`hs-opt-${i}`} role="option" aria-selected={i === highlight} className={`hs-opt${i === highlight ? " on" : ""}`}
                  onMouseEnter={() => setHighlight(i)} onMouseDown={(e) => e.preventDefault()} onClick={() => choose(r)}
                >
                  <span className="hs-ico" aria-hidden="true"><NavIcon id={r.icon} /></span>
                  <span className="hs-label">{r.label}</span>
                  {r.detail && <span className="hs-detail">{r.detail}</span>}
                </div>
              </React.Fragment>
            );
          })}
        </div>
      )}
    </div>
  );
}
