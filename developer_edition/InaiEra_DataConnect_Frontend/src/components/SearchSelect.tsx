/*
 * SearchSelect — the one dropdown used across the InaiEra apps (UCS catalog,
 * Agents, AI/ML Studio). This file and search-select.css are copied verbatim
 * into each app; change them here (reports/design/shared/) and re-copy, so the
 * three apps never drift. No app-specific imports: icons are inline, colours
 * are literal (see InaiEra_UI_Style_Spec.md §4).
 *
 * Replaces native <select>: a trigger that looks like a select, and a panel
 * with a search box, keyboard navigation,
 * optional groups / hints / descriptions, a check on the selected row, and a
 * count footer. The panel is portalled to <body> with fixed positioning, so
 * drawers, modals and scrolling cards never clip it.
 *
 * DataConnect's copy has three small changes, each marked "DataConnect" below, that belong in the shared source too: the search box takes focus
 * once the panel is placed (it did not), the list is never taller than the room, and `!` for noUncheckedIndexedAccess.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import './search-select.css';

export interface SelectOption {
  value: string;
  label: string;
  /** Shown to the right of the label — a status, a count, a kind. */
  hint?: string;
  /** A second, quieter line under the label — an address, an id. Searched too. */
  description?: string;
  /** Options sharing a group are listed under one header. Keep them adjacent. */
  group?: string;
  disabled?: boolean;
}

/** With searchable='auto', lists longer than this get the search box and step buttons. */
export const SEARCH_THRESHOLD = 6;

const Chev = ({ up }: { up?: boolean }) => (
  <svg className={`ss-chev${up ? ' up' : ''}`} viewBox="0 0 24 24" aria-hidden="true">
    <path d="m6 9 6 6 6-6" />
  </svg>
);
const SearchIcon = () => (
  <svg className="ss-ico" viewBox="0 0 24 24" aria-hidden="true">
    <circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" />
  </svg>
);
const Check = () => (
  <svg className="ss-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5L20 7" /></svg>
);

let seq = 0;

export default function SearchSelect({
  id, labelledBy, ariaLabel, title, name,
  value, options, onChange,
  placeholder = 'Select…', searchPlaceholder = 'Type to filter…',
  emptyText = 'Nothing matches', noun = 'option',
  disabled = false, size = 'md', searchable = true,
  className, style, menuMinWidth = 300,
}: {
  id?: string;
  /** Id of the visible <label>, so the trigger is named by it. */
  labelledBy?: string;
  /** Accessible name when there is no visible label. */
  ariaLabel?: string;
  title?: string;
  /** Renders a hidden input with this name, for forms read via FormData. */
  name?: string;
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  /** Singular; pluralised with an "s" for the count line. */
  noun?: string;
  disabled?: boolean;
  /** 'sm' for pagers, dense filter bars and table cells. */
  size?: 'md' | 'sm';
  /** The search box is part of the standard look and on by default; 'auto' shows it only
   *  above SEARCH_THRESHOLD options. Leave the default unless there's a reason not to. */
  searchable?: boolean | 'auto';
  className?: string;
  /** Applied to the wrapper — width / flex / margins. */
  style?: CSSProperties;
  menuMinWidth?: number;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const [pos, setPos] = useState<{ top: number; left: number; width: number; up: boolean; room: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [listId] = useState(() => `ss-list-${++seq}`);

  const withSearch = searchable === 'auto' ? options.length > SEARCH_THRESHOLD : searchable;
  const selected = options.find((o) => o.value === value);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.label.toLowerCase().includes(q)
      || (o.hint || '').toLowerCase().includes(q)
      || (o.description || '').toLowerCase().includes(q)
      || (o.group || '').toLowerCase().includes(q));
  }, [options, query]);

  // Opening lands the highlight on what is already selected; filtering moves it to the first match.
  useEffect(() => {
    if (!open) return;
    const i = shown.findIndex((o) => o.value === value);
    setActive(i >= 0 ? i : Math.max(0, shown.findIndex((o) => !o.disabled)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, query]);

  useEffect(() => {
    if (!open) { setQuery(''); return; }
    (withSearch ? searchRef.current : listRef.current)?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, withSearch, pos !== null]);   // DataConnect: also once placed — the panel (and its search box) only exists after `pos` is set

  // Keep the highlighted row on screen (optional-called: jsdom has no scrollIntoView).
  useEffect(() => {
    if (!open) return;
    listRef.current?.querySelector<HTMLElement>('[data-active="true"]')?.scrollIntoView?.({ block: 'nearest' });
  }, [active, open]);

  // Fixed position under (or above, when there's no room) the trigger; follows scroll/resize.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (!r) return;
      const below = window.innerHeight - r.bottom;
      const up = below < 320 && r.top > below;
      // DataConnect: the menu may be wider than its trigger (menuMinWidth), so keep it inside the window — a trigger near the right edge
      // (the Results range filter) would otherwise open it half off-screen.
      const menuWidth = Math.max(r.width, menuMinWidth);
      const fits = r.left + menuWidth <= window.innerWidth - 8;
      const left = Math.max(8, Math.min(fits ? r.left : r.right - menuWidth, window.innerWidth - menuWidth - 8));   // overflowing: right edges line up
      setPos({ top: up ? r.top - 6 : r.bottom + 6, left, width: r.width, up, room: (up ? r.top : below) - 6 - 12 });   // DataConnect: room = space on the chosen side
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!wrapRef.current?.contains(t) && !menuRef.current?.contains(t)) setOpen(false);
    };
    document.addEventListener('mousedown', away);
    return () => document.removeEventListener('mousedown', away);
  }, [open]);

  const move = (delta: number) => {
    if (!shown.length) return;
    setActive((i) => {
      let n = i;
      for (let step = 0; step < shown.length; step++) {
        const next = Math.min(shown.length - 1, Math.max(0, n + delta));
        if (next === n) return i;
        n = next;
        if (!shown[n]!.disabled) return n;   // DataConnect: `!` for noUncheckedIndexedAccess — the only change from the shared copy
      }
      return i;
    });
  };

  const commit = (o: SelectOption) => {
    if (o.disabled) return;
    if (o.value !== value) onChange(o.value);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (disabled) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) { setOpen(true); return; }
      move(e.key === 'ArrowDown' ? 1 : -1);
    } else if (e.key === 'Home' && open) {
      e.preventDefault(); setActive(0);
    } else if (e.key === 'End' && open) {
      e.preventDefault(); setActive(Math.max(0, shown.length - 1));
    } else if (e.key === 'Enter' || (e.key === ' ' && open && !withSearch)) {
      if (!open) return;
      e.preventDefault();
      if (shown[active]) commit(shown[active]);
    } else if (e.key === 'Escape' && open) {
      e.preventDefault(); e.stopPropagation(); setOpen(false); triggerRef.current?.focus();
    } else if (e.key === 'Tab' && open) {
      setOpen(false);
    }
  };

  const menu = open && pos && (
    // No onKeyDown here: React bubbles portal events to the wrapper below, which handles them once.
    <div ref={menuRef} className="ss-menu" role="presentation"
      style={{
        position: 'fixed', left: pos.left, width: Math.max(pos.width, menuMinWidth),
        ...(pos.up ? { bottom: window.innerHeight - pos.top } : { top: pos.top }),
      }}>
      {withSearch && (
        <div className="ss-search">
          <SearchIcon />
          <input ref={searchRef} value={query} placeholder={searchPlaceholder}
            aria-label={searchPlaceholder} aria-controls={listId}
            onChange={(e) => setQuery(e.target.value)} />
          {/* The same steps the arrow keys take, for a mouse. Mouse-down is swallowed so a
              click doesn't pull focus off the search box. */}
          <div className="ss-steppers">
            <button type="button" aria-label={`Previous ${noun}`} title="Previous (↑)" disabled={active <= 0}
              onMouseDown={(e) => e.preventDefault()} onClick={() => move(-1)}><Chev up /></button>
            <button type="button" aria-label={`Next ${noun}`} title="Next (↓)" disabled={active >= shown.length - 1}
              onMouseDown={(e) => e.preventDefault()} onClick={() => move(1)}><Chev /></button>
          </div>
        </div>
      )}
      <div className="ss-list" id={listId} ref={listRef} role="listbox" tabIndex={-1}
        style={{ maxHeight: Math.max(80, Math.min(248, pos.room - (withSearch ? 50 : 0) - 34)) }}   /* DataConnect: never taller than the room there is */
        aria-labelledby={labelledBy} aria-label={labelledBy ? undefined : ariaLabel}>
        {shown.map((o, i) => (
          <div key={`${o.group ?? ''}\u0000${o.value}`} role="presentation">
            {o.group && o.group !== shown[i - 1]?.group && (
              <div className="ss-group" role="presentation">{o.group}</div>
            )}
            <div role="option" aria-selected={o.value === value} aria-disabled={o.disabled || undefined}
              data-active={i === active}
              className={`ss-option${o.value === value ? ' sel' : ''}${i === active ? ' active' : ''}${o.disabled ? ' disabled' : ''}`}
              onMouseMove={() => { if (!o.disabled) setActive(i); }}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => commit(o)}>
              <span className="ss-option-label">
                {o.label}
                {o.description && <span className="ss-option-desc">{o.description}</span>}
              </span>
              {o.hint && <span className="ss-chip">{o.hint}</span>}
              {o.value === value && <Check />}
            </div>
          </div>
        ))}
        {!shown.length && <div className="ss-empty">{emptyText}</div>}
      </div>
      <div className="ss-foot">
        <span>{shown.length === options.length
          ? `${options.length} ${noun}${options.length === 1 ? '' : 's'}`
          : `${shown.length} of ${options.length}`}</span>
        <span className="ss-hint">↑↓ move · ↵ select · esc close</span>
      </div>
    </div>
  );

  return (
    <div ref={wrapRef} className={`ss${size === 'sm' ? ' ss-sm' : ''}${className ? ` ${className}` : ''}`}
      style={style} onKeyDown={onKeyDown}>
      <button ref={triggerRef} type="button" id={id} className="ss-trigger" title={title}
        disabled={disabled} aria-haspopup="listbox" aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-labelledby={labelledBy} aria-label={labelledBy ? undefined : ariaLabel}
        onClick={() => setOpen((v) => !v)}>
        <span className={`ss-value${selected ? '' : ' muted'}`}>{selected ? selected.label : placeholder}</span>
        {selected?.hint && size !== 'sm' && <span className="ss-chip">{selected.hint}</span>}
        <Chev up={open} />
      </button>
      {name && <input type="hidden" name={name} value={value} />}
      {menu && createPortal(menu, document.body)}
    </div>
  );
}
