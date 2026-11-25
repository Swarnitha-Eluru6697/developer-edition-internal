import { useCallback, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";

interface DragResizeOptions {
  /** "y" resizes a height from a horizontal divider, "x" a width from a vertical one. */
  axis: "x" | "y";
  /** The size at drag start. */
  current: number;
  /** Maps (start size, pointer delta toward growth) to the new size. */
  compute: (startSize: number, delta: number) => number;
  onResize: (size: number) => void;
  /** Called once with the final size on mouse-up. */
  onEnd?: (size: number) => void;
}

/**
 * Pointer-drag resizing for a panel divider. The delta is measured toward
 * growth: dragging *up* grows a bottom panel (axis "y"), dragging *left*
 * grows a right-hand panel (axis "x"). Also reports `resizing` so callers
 * can suppress CSS transitions mid-drag.
 */
export function useDragResize({ axis, current, compute, onResize, onEnd }: DragResizeOptions) {
  const [resizing, setResizing] = useState(false);

  const start = useCallback((e: ReactMouseEvent) => {
    e.preventDefault();
    const origin = axis === "y" ? e.clientY : e.clientX;
    const startSize = current;
    let latest = startSize;
    setResizing(true);
    const onMove = (ev: MouseEvent) => {
      const pos = axis === "y" ? ev.clientY : ev.clientX;
      latest = compute(startSize, origin - pos);
      onResize(latest);
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setResizing(false);
      onEnd?.(latest);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    document.body.style.cursor = axis === "y" ? "row-resize" : "col-resize";
    document.body.style.userSelect = "none";
  }, [axis, current, compute, onResize, onEnd]);

  return { start, resizing };
}
