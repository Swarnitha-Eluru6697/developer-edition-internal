/*
 * A row's actions behind one "three dots" button. Clicking it opens a small menu under the button with one line per action — its icon and its name,
 * stacked — instead of a row of bare icons. An action the row can't do right now stays in the list, greyed, with the reason on hover.
 *
 * Keyboard: Enter / Space / ArrowDown on the button opens it; Up / Down / Home / End move; Enter picks; Esc (or Tab, or a click elsewhere, or
 * scrolling) closes it and returns focus to the button. The menu is placed against the window and drawn inside the DataConnect root, so no card or
 * scrolling table around the button can clip it, and it takes DataConnect's styles and quick tooltips.
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { IconMore } from "./ui";

export interface RowMenuItem {
  key: string;
  /** What the line says. */
  label: string;
  /** The accessible name when it should say more than the line (e.g. "Deploy orders-cdc"). */
  ariaLabel?: string;
  icon: React.ReactNode;
  onSelect: () => void;
  danger?: boolean;
  /** Greyed and not pickable; `reason` is what hovering says. */
  disabled?: boolean;
  reason?: string | null;
}

const MENU_WIDTH = 200;
const GAP = 6;
const EDGE = 8;

export default function RowMenu({ label, items, busy = false }: { label: string; items: RowMenuItem[]; busy?: boolean }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top?: number; bottom?: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const root = typeof document !== "undefined" ? (buttonRef.current?.closest(".dc-root") as HTMLElement | null) ?? document.body : null;

  const close = (refocus = true) => { setOpen(false); setPos(null); if (refocus) buttonRef.current?.focus(); };

  useLayoutEffect(() => {
    if (!open) return;
    const r = buttonRef.current?.getBoundingClientRect();
    if (!r) return;
    const height = items.length * 36 + 12;
    const below = window.innerHeight - r.bottom - GAP - EDGE;
    const right = Math.max(EDGE, window.innerWidth - r.right);
    setPos(below >= height || below >= r.top - GAP - EDGE ? { top: r.bottom + GAP, right } : { bottom: window.innerHeight - r.top + GAP, right });
  }, [open, items.length]);

  // Once placed, focus the first line that can be picked.
  useEffect(() => {
    if (open && pos) menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])')?.focus();
  }, [open, pos]);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!menuRef.current?.contains(t) && !buttonRef.current?.contains(t)) close(false);
    };
    const leave = () => close(false);
    document.addEventListener("mousedown", away);
    window.addEventListener("resize", leave);
    window.addEventListener("scroll", leave, true);
    return () => { document.removeEventListener("mousedown", away); window.removeEventListener("resize", leave); window.removeEventListener("scroll", leave, true); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const move = (delta: number | "first" | "last") => {
    const lines = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? [])];
    if (!lines.length) return;
    const at = lines.indexOf(document.activeElement as HTMLElement);
    const next = delta === "first" ? 0 : delta === "last" ? lines.length - 1 : (at + delta + lines.length) % lines.length;
    lines[next]?.focus();
  };

  const onMenuKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "Home") { e.preventDefault(); move("first"); }
    else if (e.key === "End") { e.preventDefault(); move("last"); }
    else if (e.key === "Tab") close(false);
  };

  const pick = (item: RowMenuItem) => {
    if (item.disabled) return;
    close();                       // focus goes back to the button first, so a panel the action opens hands focus back to it when it closes
    item.onSelect();
  };

  return (
    <>
      <button
        ref={buttonRef} type="button" className="ai" aria-haspopup="menu" aria-expanded={open} aria-label={label} title="More actions" disabled={busy}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => { if (e.key === "ArrowDown" && !open) { e.preventDefault(); setOpen(true); } }}
      >
        {busy ? <span className="spin" /> : <IconMore />}
      </button>
      {open && root && createPortal(
        <div
          ref={menuRef} className="rm-menu" role="menu" aria-label={label} onKeyDown={onMenuKey}
          style={{ position: "fixed", width: MENU_WIDTH, right: pos?.right ?? EDGE, ...(pos?.bottom !== undefined ? { bottom: pos.bottom } : { top: pos?.top ?? -9999 }) }}
        >
          {items.map((item) => (
            <button
              key={item.key} type="button" role="menuitem" tabIndex={-1}
              className={`rm-item${item.danger ? " danger" : ""}${item.disabled ? " off" : ""}`}
              aria-label={item.ariaLabel} aria-disabled={item.disabled || undefined} title={item.disabled && item.reason ? item.reason : undefined}
              onClick={() => pick(item)}
            >
              <span className="rm-ico" aria-hidden="true">{item.icon}</span>
              <span className="rm-txt">{item.label}</span>
            </button>
          ))}
        </div>,
        root,
      )}
    </>
  );
}
