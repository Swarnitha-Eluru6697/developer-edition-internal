import { useEffect, type RefObject } from "react";
import { useLatest } from "./useLatest";

/** Calls `onOutside` on a mousedown outside `ref` while `active`. */
export function useClickOutside(ref: RefObject<HTMLElement>, active: boolean, onOutside: () => void) {
  const handler = useLatest(onOutside);
  useEffect(() => {
    if (!active) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) handler.current();
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [active, ref, handler]);
}
