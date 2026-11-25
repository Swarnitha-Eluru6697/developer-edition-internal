/** Compact relative age: "42s ago", "5m ago", "3h ago", "2d ago". */
export function timeAgo(isoDate: string, now: number = Date.now()): string {
  const ms = now - new Date(isoDate).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}
