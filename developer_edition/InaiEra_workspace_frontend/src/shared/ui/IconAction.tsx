/**
 * The one icon-only action button (view, edit, delete, close, refresh…),
 * matching .icon-action in the other InAiEra apps: white, bordered, gray glyph;
 * `danger` turns red on hover. Always carries a title + aria-label naming the action.
 *
 * The refresh button answers a click with ONE quick blink of its arrow (like the browser's reload button, which just reacts once and
 * gets on with it): no spinning, nothing that waits for the refresh to finish.
 */
import { useState, type CSSProperties, type MouseEvent } from "react";
import "./icon-action.css";
import { Glyph } from "./Glyph";

/** Standard glyphs, so every screen uses the same symbol for the same action. */
export const ACTION_ICON = {
  view: "pi-eye", edit: "pi-pencil", delete: "pi-trash", close: "pi-x", refresh: "pi-reload", download: "pi-download", copy: "pi-copy",
} as const;

interface Props {
  /** A platform glyph (ACTION_ICON.delete) or a Tabler class ("ti-file-plus"). */
  icon: string;
  /** What the button does — used for both the tooltip and the accessible name. */
  label: string;
  onClick: (e: MouseEvent<HTMLButtonElement>) => unknown;
  danger?: boolean;
  /** Toggled-on state (e.g. a panel the button opened). */
  on?: boolean;
  size?: "md" | "sm";
  disabled?: boolean;
  style?: CSSProperties;
}

export function IconAction({ icon, label, onClick, danger, on, size = "md", disabled, style }: Props) {
  const isRefresh = icon === ACTION_ICON.refresh;
  const [blinks, setBlinks] = useState(0);              // each click renders a fresh glyph, which plays the blink once
  const handleClick = (e: MouseEvent<HTMLButtonElement>) => {
    if (isRefresh) setBlinks((n) => n + 1);
    onClick(e);
  };
  return (
    <button
      type="button"
      className={`icon-action${size === "sm" ? " sm" : ""}${danger ? " danger" : ""}${on ? " on" : ""}`}
      title={label}
      aria-label={label}
      aria-pressed={on}
      onClick={handleClick}
      disabled={disabled}
      style={style}
    >
      <span key={blinks} className={blinks ? "icon-action-blink" : undefined} style={{ display: "grid" }}>
        <Glyph name={icon} size={size === "sm" ? 13 : 15} />
      </span>
    </button>
  );
}
