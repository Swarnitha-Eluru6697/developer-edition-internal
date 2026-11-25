import type { EngineState } from "../../types";

/** How each live state is worded and coloured on a Settings card. */
export const STATE: Record<EngineState, { label: string; dot: string; text: string }> = {
  connected: { label: "connected", dot: "#22c55e", text: "#2f6b14" },
  unreachable: { label: "unreachable", dot: "#ef4444", text: "#b23b3b" },
  not_configured: { label: "not configured", dot: "#f59e0b", text: "#a8470a" },
  disabled: { label: "disabled", dot: "#73827a", text: "#5b6d62" },
};
