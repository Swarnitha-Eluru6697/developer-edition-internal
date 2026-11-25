import type { MouseEvent } from "react";
import { Icon } from "./Icons";
import { usePermissions } from "../context/PermissionsContext";

/**
 * Icon-only action button named by a hover tooltip, for per-item actions
 * (edit, delete, test, view…).
 *
 * `label` is the full operation ("Delete channel ml-alerts"); it becomes both
 * the tooltip (`data-tip`) and the `aria-label`, and there's no `title`, so
 * the browser's own tooltip never doubles up on it.
 *
 * `gated` names the permission the action needs (e.g. "mlstudio:alerts:edit"):
 * without it — or while permissions are still loading — the button is not
 * rendered at all. The backend checks the same permission on the request.
 *
 * Placement: below and centred by default; `tip="up"` for rows inside an
 * overflow-clipped table (the last row's bubble would be cut off below), and
 * `align="end"` for buttons near a right edge.
 */
export default function IconAction({
  icon, label, onClick, danger, disabled, gated, size, tip, align, className,
}: {
  icon: string;
  label: string;
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void;
  danger?: boolean;
  disabled?: boolean;
  gated?: string;
  size?: "sm";
  tip?: "up";
  align?: "end" | "start";
  className?: string;
}) {
  const { can } = usePermissions();
  if (gated && !can(gated)) return null;
  const cls = ["icon-action", "has-tip", danger && "danger", size,
    tip === "up" && "tip-up", align && `tip-${align}`, className].filter(Boolean).join(" ");
  return (
    <button
      type="button"
      className={cls}
      disabled={disabled}
      aria-label={label}
      data-tip={label}
      onClick={onClick}
    >
      <Icon name={icon} />
    </button>
  );
}
