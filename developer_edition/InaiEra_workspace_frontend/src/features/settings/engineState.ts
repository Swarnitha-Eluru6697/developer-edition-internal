import type { EngineState } from "../../types";

/** How each live state is worded and coloured on a Settings card. */
export const STATE: Record<EngineState, { label: string; dot: string; text: string }> = {
  connected: { label: "connected", dot: "#22c55e", text: "#15803d" },
  unreachable: { label: "unreachable", dot: "#ef4444", text: "#b91c1c" },
  not_configured: { label: "not configured", dot: "#f59e0b", text: "#b45309" },
  disabled: { label: "disabled", dot: "#9ca3af", text: "#6b7280" },
};
