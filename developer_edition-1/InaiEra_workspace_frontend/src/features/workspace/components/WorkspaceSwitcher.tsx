/**
 * "My Workspace ▾" — pick, search, or create (blank or from Git) a workspace.
 * The panel uses the shared SearchSelect look (shared/ui/search-select.css):
 * filter box, ↑↓/↵/esc, a check on the current workspace and a count footer.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "../../../shared/ui/search-select.css";
import { useDispatch } from "react-redux";
import { openGitModal } from "../../../store/uiSlice";
import type { AppDispatch } from "../../../store";
import type { UUID, Workspace } from "../../../types";
import { filterWorkspaces, workspaceLabel } from "../workspaces";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";
import { useHasPermission } from "../../../rbac/hooks";

interface Props {
  workspaces: Workspace[] | undefined;
  activeWs: UUID | null;
  onSelect: (id: UUID) => void;
  onNewWorkspace: () => void;
  /** Open the Add New file menu for the active workspace. */
  onAddFile: () => void;
  /** Rename the active workspace. */
  onRenameWorkspace: () => void;
  /** Delete the active workspace (asks for its name first). */
  onDeleteWorkspace: () => void;
}

const hover = {
  onMouseEnter: (e: React.MouseEvent<HTMLDivElement>) => (e.currentTarget.style.background = "#F9FAFB"),
  onMouseLeave: (e: React.MouseEvent<HTMLDivElement>) => (e.currentTarget.style.background = "transparent"),
};

export function WorkspaceSwitcher({ workspaces, activeWs, onSelect, onNewWorkspace, onAddFile, onRenameWorkspace, onDeleteWorkspace }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  // Creating, renaming and deleting workspaces (and adding files) is workspace:files:edit.
  const canEditFiles = useHasPermission("workspace:files:edit");
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  // ⋯ → a row of icon-only actions on the active workspace: add file, edit (rename), delete.
  const [actionsOpen, setActionsOpen] = useState(false);
  const actionsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!actionsOpen) return;
    const away = (e: MouseEvent) => { if (!actionsRef.current?.contains(e.target as Node)) setActionsOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setActionsOpen(false); };
    document.addEventListener("mousedown", away);
    window.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); window.removeEventListener("keydown", esc); };
  }, [actionsOpen]);
  const act = (fn: () => void) => () => { setActionsOpen(false); setOpen(false); fn(); };
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  // The file panel is 225px wide and clips its overflow, so the list is portalled
  // to <body> at a fixed position under the trigger (like SearchSelect) — inside the
  // panel it was cut off, and focusing its filter box scrolled the whole panel sideways.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (r) setPos({ top: r.bottom + 6, left: r.left });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => { window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!ref.current?.contains(t) && !menuRef.current?.contains(t)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const shown = filterWorkspaces(workspaces, search);
  const total = workspaces?.length ?? 0;
  // Opening lands on the current workspace; filtering moves to the first match.
  useEffect(() => {
    if (!open) { setSearch(""); return; }
    const i = shown.findIndex((w) => w.id === activeWs);
    setActive(i >= 0 ? i : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, search]);
  const pick = (id: UUID) => { onSelect(id); setOpen(false); };
  const onSearchKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(i + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); if (shown[active]) pick(shown[active].id); }
    else if (e.key === "Escape") { e.preventDefault(); setOpen(false); }
  };

  return (
    <div ref={ref}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <button
          ref={triggerRef}
          onClick={() => setOpen(!open)}
          style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 600, background: "none", border: "none", padding: 0, cursor: "pointer", color: "#1F2937" }}
        >
          <i className="ti ti-folder" style={{ fontSize: 14, color: "#F97316" }} />
          {(() => {
            const label = workspaceLabel(workspaces, activeWs);
            if (label.kind === "name") return label.text;
            // Loading: a quiet bar, not a made-up name. Loaded with none: say so.
            return label.kind === "loading"
              ? <span aria-label="Loading workspace" style={{ display: "inline-block", width: 96, height: 10, borderRadius: 4, background: "#F3F4F6" }} />
              : <span style={{ color: "#9ca3af", fontWeight: 400 }}>No workspace</span>;
          })()}
          <i className="ti ti-chevron-down" style={{ fontSize: 11, color: "#9ca3af" }} />
        </button>
        {canEditFiles && <div ref={actionsRef} style={{ position: "relative" }}>
          <IconAction size="sm" icon="ti-dots" label="Workspace actions" on={actionsOpen}
            onClick={() => { setActionsOpen((o) => !o); setOpen(false); }} />
          {actionsOpen && (
            <div role="menu" aria-label="Workspace actions"
              style={{ position: "absolute", top: 30, right: 0, display: "flex", gap: 6, padding: 6, background: "#fff", border: "1px solid #E5E7EB", borderRadius: 10, boxShadow: "0 12px 40px rgba(16,32,58,.18)", zIndex: 120 }}>
              <IconAction icon="ti-file-plus" label="Add file" disabled={!activeWs} onClick={act(onAddFile)} />
              <IconAction icon={ACTION_ICON.edit} label="Rename workspace" disabled={!activeWs} onClick={act(onRenameWorkspace)} />
              <IconAction icon={ACTION_ICON.delete} label="Delete workspace" danger disabled={!activeWs} onClick={act(onDeleteWorkspace)} />
            </div>
          )}
        </div>}
      </div>

      {open && pos && createPortal(
        <div ref={menuRef} className="ss-menu" style={{ position: "fixed", top: pos.top, left: pos.left, width: 300 }}>
          <div className="ss-search">
            <svg className="ss-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
            <input
              autoFocus
              placeholder="Type to filter…"
              aria-label="Filter workspaces"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={onSearchKey}
            />
          </div>
          <div className="ss-list" role="listbox" aria-label="Workspaces">
            <div className="ss-group">Workspaces</div>
            {shown.map((w, i) => (
              <div
                key={w.id}
                role="option"
                aria-selected={w.id === activeWs}
                className={`ss-option${w.id === activeWs ? " sel" : ""}${i === active ? " active" : ""}`}
                onMouseMove={() => setActive(i)}
                onClick={() => pick(w.id)}
              >
                <i className="ti ti-layout-board-split" style={{ fontSize: 14, color: "#F97316" }} />
                <span className="ss-option-label">{w.name}</span>
                {w.id === activeWs && <svg className="ss-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5L20 7" /></svg>}
              </div>
            ))}
            {!shown.length && <div className="ss-empty">Nothing matches</div>}
            {canEditFiles && (<>
            <div className="ss-group" style={{ borderTop: "1px solid #F3F4F6", marginTop: 4 }}>Create workspace</div>
            <div className="ss-option" onClick={() => { setOpen(false); onNewWorkspace(); }} {...hover}>
              <i className="ti ti-plus" style={{ fontSize: 14, color: "#F97316" }} />
              <span className="ss-option-label">New</span>
            </div>
            <div className="ss-option" onClick={() => { dispatch(openGitModal()); setOpen(false); }} {...hover}>
              <i className="ti ti-git-fork" style={{ fontSize: 14, color: "#F97316" }} />
              <span className="ss-option-label">From Git repository</span>
            </div>
            </>)}
          </div>
          <div className="ss-foot">
            <span>{shown.length === total ? `${total} workspace${total === 1 ? "" : "s"}` : `${shown.length} of ${total}`}</span>
            <span className="ss-hint">↑↓ move · ↵ select · esc close</span>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}
