/**
 * "Add New ▾" — pick the file kind rather than silently creating a .sql.
 * The list uses the shared SearchSelect look (shared/ui/search-select.css): a
 * filter box, ↑↓ / ↵ / esc, a description line per kind and a count footer.
 * It is portalled to <body> at a fixed position, because the 225px file panel
 * clips anything wider than itself.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { usePermissions } from "../../../rbac/hooks";
import "../../../shared/ui/search-select.css";
import { allowedNewFileOptions, type NewFileLanguage, type NewFileOption } from "../newFiles";

interface Props {
  onNewFile: (language: NewFileLanguage) => void;
  onNewDbtProject: () => void;
  /** Bump to open the menu from elsewhere (the workspace ⋯ → Add file). */
  openSignal?: number;
}

const MENU_WIDTH = 300;

export function AddNewMenu({ onNewFile, onNewDbtProject, openSignal = 0 }: Props) {
  const permissions = usePermissions();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const options = allowedNewFileOptions(permissions);
  const q = query.trim().toLowerCase();
  const shown = q ? options.filter((o) => `${o.label} ${o.hint}`.toLowerCase().includes(q)) : options;

  useEffect(() => { if (openSignal > 0) setOpen(true); }, [openSignal]);
  useEffect(() => { if (!open) setQuery(""); setActive(0); }, [open, query]);

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
      if (!wrapRef.current?.contains(t) && !menuRef.current?.contains(t)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const choose = (o: NewFileOption | undefined) => {
    if (!o) return;
    setOpen(false);
    if (o.kind === "dbt") onNewDbtProject();
    else onNewFile(o.kind);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(i + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); choose(shown[active]); }
    else if (e.key === "Escape") { e.preventDefault(); setOpen(false); triggerRef.current?.focus(); }
  };

  // Nothing this user may create (no workspace:files:edit): no menu at all.
  if (options.length === 0) return null;

  return (
    <div ref={wrapRef} style={{ position: "relative" }}>
      <button
        ref={triggerRef}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        style={{ height: 30, width: "100%", display: "flex", alignItems: "center", justifyContent: "center", gap: 6, fontSize: 12.5, fontWeight: 600, border: `1px solid ${open ? "#0A7D5A" : "var(--ws-line, #E3E8E2)"}`, boxShadow: open ? "0 0 0 3px var(--ws-accent-bg, #E3F6EA)" : "none", borderRadius: 8, background: "var(--ws-surface, #fff)", cursor: "pointer", color: open ? "var(--ws-accent, #086347)" : "var(--ws-text-2, #2E3D36)" }}
      >
        <i className="ti ti-plus" style={{ fontSize: 13 }} />Add New
        <i className="ti ti-chevron-down" style={{ fontSize: 11, color: "var(--ws-subtle, #73827A)", transform: open ? "rotate(180deg)" : "none", transition: "transform .12s" }} />
      </button>

      {open && pos && createPortal(
        <div ref={menuRef} className="ss-menu" style={{ position: "fixed", top: pos.top, left: pos.left, width: MENU_WIDTH }} onKeyDown={onKeyDown}>
          <div className="ss-search">
            <svg className="ss-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
            <input autoFocus value={query} placeholder="Type to filter…" aria-label="Filter file types" onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div className="ss-list" role="listbox" aria-label="New file type">
            <div className="ss-group">Create</div>
            {shown.map((o, i) => (
              <div
                key={o.kind}
                role="option"
                aria-selected={i === active}
                className={`ss-option${i === active ? " active" : ""}`}
                style={{ alignItems: "flex-start" }}
                onMouseMove={() => setActive(i)}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => choose(o)}
              >
                <i className={`ti ${o.icon}`} style={{ fontSize: 15, color: o.color, marginTop: 1 }} />
                <span className="ss-option-label">
                  {o.label}
                  <span className="ss-option-desc">{o.hint}</span>
                </span>
              </div>
            ))}
            {!shown.length && <div className="ss-empty">Nothing matches</div>}
          </div>
          <div className="ss-foot">
            <span>{shown.length === options.length ? `${options.length} file type${options.length === 1 ? "" : "s"}` : `${shown.length} of ${options.length}`}</span>
            <span className="ss-hint">↑↓ move · ↵ select · esc close</span>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}
