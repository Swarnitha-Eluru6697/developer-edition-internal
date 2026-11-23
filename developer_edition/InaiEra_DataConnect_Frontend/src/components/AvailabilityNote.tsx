import { availabilityNote, type Availability } from "../availability";

/**
 * NEW-09: why a chosen option is not "supported" — the backend's own message and what to do about it. Nothing when it is supported or when the
 * backend said nothing. `id` is for aria-describedby on the control it explains.
 */
export default function AvailabilityNote({ id, availability, advisory = false }: { id?: string; availability: Availability | undefined; advisory?: boolean }) {
  const found = availabilityNote(availability);
  if (!found) return null;
  // `advisory`: a heads-up that never blocks (the connector picker), so an unavailable option reads as a warning, not an error.
  const note = advisory && found.tone === "error" ? { ...found, tone: "warning" as const, title: "Heads up" } : found;
  return (
    <div id={id} className={note.tone === "error" ? "alert alert-e" : "alert alert-w"} role={note.tone === "error" ? "alert" : "status"}
      data-availability-code={note.code ?? undefined}>
      <div className="at">{note.title}</div>
      <div className="ab">{note.text}</div>
    </div>
  );
}
