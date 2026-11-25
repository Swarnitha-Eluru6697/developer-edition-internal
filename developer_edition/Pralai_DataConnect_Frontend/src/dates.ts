/*
 * "Created at" on the lists and detail panels: the time the backend recorded (an ISO timestamp), shown in the viewer's own time zone,
 * e.g. "Oct 22, 2026, 7:53 AM". A missing or unreadable value is a dash, never "Invalid Date". The full time with seconds is in the tooltip.
 */
export function parseTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatCreatedAt(value: string | null | undefined): string {
  const d = parseTime(value);
  return d ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
}

export function createdAtTitle(value: string | null | undefined): string | undefined {
  const d = parseTime(value);
  return d ? d.toLocaleString(undefined, { dateStyle: "full", timeStyle: "medium" }) : undefined;
}
