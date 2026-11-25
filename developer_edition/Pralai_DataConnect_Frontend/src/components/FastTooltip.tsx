/*
 * Quick tooltips. The browser shows a `title` tooltip only after about a second, which makes the icon buttons feel unresponsive. This takes
 * over the `title` of whatever the pointer (or keyboard focus) rests on inside DataConnect and shows it after ~120ms in the same dark chip
 * (and instantly when moving between neighbouring icons). While it is showing, the element's own `title` is set aside so the browser's
 * slow tooltip doesn't appear as well; it is put back when the pointer leaves, on click, or when the tooltip goes away.
 * Accessible names are unaffected: every icon button also has an aria-label.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";

const SHOW_DELAY_MS = 120;
const GRACE_MS = 300;                       // moving to a neighbouring icon within this time shows its tip at once

interface Tip { text: string; x: number; y: number; above: boolean }

export default function FastTooltip() {
  const [tip, setTip] = useState<Tip | null>(null);
  const current = useRef<{ el: HTMLElement; title: string } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastHidden = useRef(0);

  const restore = useCallback(() => {
    const c = current.current;
    if (c && !c.el.hasAttribute("title")) c.el.setAttribute("title", c.title);
    current.current = null;
  }, []);
  const hide = useCallback(() => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    if (current.current) lastHidden.current = Date.now();
    restore();
    setTip(null);
  }, [restore]);

  const show = useCallback((el: HTMLElement) => {
    if (current.current?.el === el) return;
    hide();
    const title = el.getAttribute("title");
    if (!title) return;
    current.current = { el, title };
    el.removeAttribute("title");            // keeps the browser's slow tooltip from also appearing
    const place = () => {
      if (current.current?.el !== el || !el.isConnected) return;
      const r = el.getBoundingClientRect();
      const above = r.bottom + 40 > window.innerHeight;
      setTip({ text: title, x: Math.min(Math.max(r.left + r.width / 2, 16), window.innerWidth - 16), y: above ? r.top - 6 : r.bottom + 6, above });
    };
    if (Date.now() - lastHidden.current < GRACE_MS) place();
    else timer.current = setTimeout(place, SHOW_DELAY_MS);
  }, [hide]);

  useEffect(() => {
    const inRoot = (t: EventTarget | null) => (t instanceof Element && t.closest(".dc-root") ? t : null);
    const titled = (t: EventTarget | null) => inRoot(t)?.closest<HTMLElement>("[title]") ?? null;
    const over = (e: MouseEvent) => {
      const el = titled(e.target);
      if (el) show(el);
      else if (current.current && !(e.target instanceof Node && current.current.el.contains(e.target))) hide();
    };
    const focus = (e: FocusEvent) => { const el = titled(e.target); if (el && (e.target as HTMLElement).matches(":focus-visible")) show(el); };
    const out = (e: MouseEvent) => {
      const c = current.current;
      if (c && !(e.relatedTarget instanceof Node && c.el.contains(e.relatedTarget))) hide();
    };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };
    document.addEventListener("mouseover", over);
    document.addEventListener("mouseout", out);
    document.addEventListener("focusin", focus);
    document.addEventListener("focusout", hide);
    document.addEventListener("mousedown", hide, true);
    document.addEventListener("keydown", key);
    window.addEventListener("scroll", hide, true);
    return () => {
      document.removeEventListener("mouseover", over);
      document.removeEventListener("mouseout", out);
      document.removeEventListener("focusin", focus);
      document.removeEventListener("focusout", hide);
      document.removeEventListener("mousedown", hide, true);
      document.removeEventListener("keydown", key);
      window.removeEventListener("scroll", hide, true);
      if (timer.current) clearTimeout(timer.current);
      restore();
    };
  }, [show, hide, restore]);

  if (!tip) return null;
  return (
    <div className={`fast-tip${tip.above ? " above" : ""}`} role="tooltip" style={{ left: tip.x, top: tip.y }}>{tip.text}</div>
  );
}
