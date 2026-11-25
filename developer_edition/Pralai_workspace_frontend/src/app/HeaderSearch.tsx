/**
 * The header search box, styled like the other InAiEra apps' top-bar search.
 * Matches files in the active workspace, the workspaces themselves, the jobs
 * (dbt / SQL / Python / Flink) and the pages in the sidebar; Enter (or a click) opens the highlighted result.
 * "/" focuses it from anywhere outside a text field — ⌘K is already the
 * codegen chat toggle here.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useDispatch } from "react-redux";
import { useListFilesQuery } from "../features/workspace/api";
import { baseName, fileIcon } from "../features/workspace/fileTree";
import { usePermissions } from "../rbac/hooks";
import { useClickOutside } from "../shared/hooks/useClickOutside";
import { useListDbtProjectsQuery } from "../features/dbt/api";
import { JOB_KINDS, kindOf, type JobKind } from "../features/dbt/jobKinds";
import { useListFlinkJobsQuery } from "../features/flink/api";
import { openTab, setNavSection, showJob, type NavSection } from "../store/uiSlice";
import type { AppDispatch } from "../store";
import type { UUID, Workspace } from "../types";
import { canOpenSection, visibleNavSections } from "./navigation";
import { Glyph } from "../shared/ui/Glyph";

interface Props {
  workspaces: Workspace[] | undefined;
  activeWs: UUID | null;
  onSelectWorkspace: (id: UUID) => void;
}

interface Result { key: string; group: string; icon: string; label: string; detail?: string; run: () => void }

const MAX_FILES = 6;
const MAX_JOBS = 8;
const JOB_SECTION: Record<JobKind, NavSection> = { dbt: "dbt", sql: "sqljobs", python: "pythonjobs" };
const JOB_GROUP: Record<JobKind, string> = { dbt: "dbt jobs", sql: "SQL jobs", python: "Python jobs" };
const MAX_WORKSPACES = 4;

export function HeaderSearch({ workspaces, activeWs, onSelectWorkspace }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const permissions = usePermissions();
  const { data: files } = useListFilesQuery({ workspaceId: activeWs ?? "" }, { skip: !activeWs });
  // The jobs of the pages this user may open (the same rule as the sidebar), so the search never asks for a list it would be refused.
  // The job pages this user may open, as a stable key ("dbt,sqljobs"); the Runner keeps all three kinds of job in one list.
  const jobSections = (["dbt", "sqljobs", "pythonjobs"] as const).filter((s) => canOpenSection(s, permissions)).join(",");
  const { data: allProjects } = useListDbtProjectsQuery({}, { skip: !jobSections });
  const { data: flinkJobs } = useListFlinkJobsQuery(undefined, { skip: !canOpenSection("flink", permissions) });
  const projects = useMemo(
    () => allProjects?.filter((p) => jobSections.split(",").includes(JOB_SECTION[kindOf(p)])),
    [allProjects, jobSections],
  );
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  useClickOutside(boxRef, open, () => setOpen(false));

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName))) return;
      e.preventDefault();
      inputRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const results = useMemo<Result[]>(() => {
    const term = q.trim().toLowerCase();
    if (!term) return [];
    const fileHits: Result[] = (files ?? [])
      .filter((f) => f.path.toLowerCase().includes(term))
      .slice(0, MAX_FILES)
      .map((f) => ({
        key: `f:${f.id}`, group: "Files", icon: fileIcon(baseName(f.path), f.language).icon,
        label: baseName(f.path), detail: f.path,
        run: () => { dispatch(setNavSection("ws")); dispatch(openTab(f.id)); },
      }));
    const wsHits: Result[] = (workspaces ?? [])
      .filter((w) => w.name.toLowerCase().includes(term))
      .slice(0, MAX_WORKSPACES)
      .map((w) => ({
        key: `w:${w.id}`, group: "Workspaces", icon: "ti-folder", label: w.name,
        detail: w.id === activeWs ? "current" : undefined,
        run: () => { dispatch(setNavSection("ws")); onSelectWorkspace(w.id); },
      }));
    // Jobs: by name, engine or Git URL ("trino" finds the jobs that run on Trino); the page opens on that job.
    const jobHits: Result[] = [
      ...(projects ?? []).filter((p) => [p.name, p.engine, p.git_url].some((v) => String(v ?? "").toLowerCase().includes(term))).map((p) => {
        const kind = kindOf(p);
        return { key: `j:${p.id}`, group: JOB_GROUP[kind], icon: JOB_KINDS[kind].icon, label: p.name, detail: p.engine,
          run: () => dispatch(showJob({ section: JOB_SECTION[kind], id: p.id })) };
      }),
      ...(flinkJobs ?? []).filter((j) => [j.name, j.git_url].some((v) => String(v ?? "").toLowerCase().includes(term))).map((j) => ({
        key: `j:${j.id}`, group: "Flink jobs", icon: "ti-wave-sine", label: j.name, detail: undefined,
        run: () => dispatch(showJob({ section: "flink", id: j.id })),
      })),
    ].slice(0, MAX_JOBS);
    const pageHits: Result[] = visibleNavSections(permissions)
      .flatMap((g) => g.items.map((i) => ({ ...i, group: g.label })))
      .filter((i) => i.label.toLowerCase().includes(term))
      .map((i) => ({
        key: `p:${i.id}`, group: "Pages", icon: i.icon, label: i.label, detail: i.group,
        run: () => dispatch(setNavSection(i.id)),
      }));
    return [...fileHits, ...wsHits, ...jobHits, ...pageHits];
  }, [q, files, projects, flinkJobs, workspaces, activeWs, permissions, dispatch, onSelectWorkspace]);

  const choose = (r: Result | undefined) => {
    if (!r) return;
    r.run();
    setQ("");
    setOpen(false);
    inputRef.current?.blur();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setHighlight((h) => Math.min(h + 1, results.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setHighlight((h) => Math.max(h - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); choose(results[highlight]); }
    else if (e.key === "Escape") { setOpen(false); inputRef.current?.blur(); }
  };

  return (
    <div ref={boxRef} style={{ position: "relative", flex: "0 1 520px", minWidth: 0 }}>
      <div style={{ height: 38, display: "flex", alignItems: "center", gap: 9, background: "var(--ws-bg, #F8FAF9)", border: "1px solid var(--ws-line, #E3E8E2)", borderRadius: 9, padding: "0 12px", color: "var(--ws-muted, #5B6D62)" }}>
        <i className="ti ti-search" style={{ fontSize: 15 }} />
        <input
          ref={inputRef}
          value={q}
          placeholder="Search files, jobs, workspaces or pages…"
          aria-label="Search files, jobs, workspaces or pages"
          onChange={(e) => { setQ(e.target.value); setHighlight(0); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          style={{ flex: 1, minWidth: 0, border: 0, background: "none", outline: "none", fontSize: 13.5, color: "var(--ws-text, #14201B)" }}
        />
        <kbd style={{ fontFamily: "var(--mono)", fontSize: 11, background: "var(--ws-surface, #fff)", border: "1px solid var(--ws-line, #E3E8E2)", borderRadius: 5, padding: "1px 6px", color: "var(--ws-subtle, #73827A)" }}>/</kbd>
      </div>

      {open && q.trim() && (
        <div style={{ position: "absolute", top: 44, left: 0, right: 0, background: "var(--ws-surface, #fff)", border: "1px solid var(--ws-line, #E3E8E2)", borderRadius: 10, boxShadow: "0 12px 40px rgba(11,59,46,.18)", zIndex: 220, padding: "4px 0", maxHeight: 380, overflow: "auto" }}>
          {results.length === 0 && <div style={{ padding: "10px 14px", fontSize: 12.5, color: "var(--ws-subtle, #73827A)" }}>No matches for “{q.trim()}”</div>}
          {results.map((r, i) => (
            <div key={r.key}>
              {(i === 0 || results[i - 1].group !== r.group) && (
                <div style={{ fontSize: 10.5, fontWeight: 600, color: "var(--ws-subtle, #73827A)", textTransform: "uppercase", letterSpacing: ".12em", padding: "8px 14px 4px" }}>{r.group}</div>
              )}
              <div
                onMouseDown={(e) => { e.preventDefault(); choose(r); }}
                onMouseEnter={() => setHighlight(i)}
                style={{ display: "flex", alignItems: "center", gap: 9, padding: "7px 14px", cursor: "pointer", fontSize: 13, background: i === highlight ? "var(--ws-accent-bg, #E3F6EA)" : "transparent", color: i === highlight ? "var(--ws-accent, #086347)" : "var(--ws-text, #14201B)" }}
              >
                <Glyph name={r.icon} size={15} style={{ color: "var(--ws-accent, #0A7D5A)" }} />
                <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.label}</span>
                {r.detail && <span style={{ marginLeft: "auto", fontSize: 11.5, color: "var(--ws-subtle, #73827A)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: "50%" }}>{r.detail}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
