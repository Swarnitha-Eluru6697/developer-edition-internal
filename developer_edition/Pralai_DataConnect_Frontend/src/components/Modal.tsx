/*
 * The single right-anchored drawer shell. Reads the current modal node from
 * context and animates it in/out. Clicking the dimmed backdrop closes it.
 *
 * FE-BUG-13: closeModal() itself (not this component) decides whether a
 * close attempt is actually allowed to proceed — see AppContext.tsx's
 * ModalProvider. When the open form has unsaved changes, closeModal() raises
 * `closePrompt` instead of closing, and this component renders an "Unsaved
 * changes" confirmation over the still-mounted drawer content rather than
 * replacing it, so "Continue editing" has the exact in-progress form to
 * return to.
 *
 * FE-BUG-21: every dialog in the app goes through this one shell (via
 * Drawer, src/components/ui.tsx), so this is the single place that gives
 * all of them, at once: role="dialog"/aria-modal + aria-labelledby (Drawer
 * gives its title element the fixed id this points to — there's only ever
 * one modal open at a time, so a fixed id is safe), a real Tab/Shift+Tab
 * focus trap, Escape-to-close (routed through the exact same closeModal()
 * the × icon and backdrop click already use, so an in-progress edit still
 * gets the "Unsaved changes" prompt rather than silently discarding it),
 * and focus restoration to whatever triggered the modal once it's fully
 * closed.
 */
import React, { useEffect, useRef } from "react";
import { useApp } from "../context/AppContext";

const FOCUSABLE_SELECTOR = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export default function Modal() {
  const { modal, modalOn, closeModal, closePrompt, cancelClose, discardAndClose } = useApp();
  const dialogRef = useRef<HTMLDivElement>(null);
  const guardRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  // Capture the triggering element once, the moment a modal first opens
  // (never overwritten by a later closePrompt toggle), and restore focus to
  // it only once the modal is fully gone — not mid-fade-out.
  useEffect(() => {
    if (modal) {
      if (!previouslyFocused.current) previouslyFocused.current = document.activeElement as HTMLElement | null;
    } else if (previouslyFocused.current) {
      previouslyFocused.current.focus?.();
      previouslyFocused.current = null;
    }
  }, [modal]);

  // Move focus into whichever dialog is currently on top — the "Unsaved
  // changes" guard, when it's showing, else the drawer itself — every time
  // that changes.
  useEffect(() => {
    const container = closePrompt ? guardRef.current : dialogRef.current;
    if (!container) return;
    const focusable = container.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
    (focusable ?? container).focus();
  }, [modal, closePrompt]);

  useEffect(() => {
    if (!modal) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        // On the guard, Escape backs out of closing (the same safe default
        // as its own "Continue editing" button) rather than re-raising the
        // same prompt again.
        if (closePrompt) cancelClose(); else closeModal();
        return;
      }
      if (e.key !== "Tab") return;
      const container = closePrompt ? guardRef.current : dialogRef.current;
      if (!container) return;
      const focusables = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
        .filter((el) => !el.hasAttribute("disabled"));
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!first || !last) return;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [modal, closePrompt, closeModal, cancelClose]);

  if (!modal) return null;
  return (
    <div
      className={`ov${modalOn ? " on" : ""}`}
      onMouseDown={(e) => { if (e.target === e.currentTarget) closeModal(); }}
    >
      <div
        className={`modal${modal.wide ? " wide" : ""}`}
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        tabIndex={-1}
      >
        {modal.node}
      </div>
      {closePrompt && (
        <div className="unsaved-guard" onMouseDown={(e) => e.stopPropagation()}>
          <div
            className="unsaved-guard-box"
            ref={guardRef}
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="unsaved-guard-title"
            tabIndex={-1}
          >
            <div className="unsaved-guard-title" id="unsaved-guard-title">Unsaved changes</div>
            <div className="unsaved-guard-body">{closePrompt.message}</div>
            <div className="unsaved-guard-actions">
              <button className="btn-s" onClick={discardAndClose}>Discard</button>
              <button className="btn-p" onClick={cancelClose}>Continue editing</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
