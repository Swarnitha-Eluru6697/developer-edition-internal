/*
 * BUG-10: what the backend recorded about auto-introspecting a pipeline's source schema at creation
 * (Pipeline.schema_introspection_status / _message, written by PipelineService.autocreate_schema_from_source).
 * Pipeline creation succeeds either way; this is where the outcome stays visible after the creation toast is gone.
 * One mapping for the detail drawer and any other surface, so the vocabulary can't drift.
 */

export interface SchemaIntrospectionView {
  label: string;
  /** `badge` class — same palette as the runtime pills (bg green, bb blue, br red, bgr grey). */
  cls: string;
  /** The recorded reason, when there is one. */
  message: string | null;
  /** A failure the user should act on (as opposed to an expected skip). */
  isFailure: boolean;
}

const VIEWS: Record<string, Omit<SchemaIntrospectionView, "message">> = {
  created: { label: "AUTO-REGISTERED", cls: "bg", isFailure: false },
  linked_existing: { label: "LINKED TO EXISTING", cls: "bb", isFailure: false },
  skipped: { label: "SKIPPED", cls: "bgr", isFailure: false },
  failed: { label: "FAILED", cls: "br", isFailure: true },
};

/** null when introspection was never attempted (a schema was linked by hand, or the pipeline predates the field). */
export function schemaIntrospectionView(
  status: string | null | undefined,
  message: string | null | undefined,
): SchemaIntrospectionView | null {
  if (!status) return null;
  const known = VIEWS[status];
  if (!known) return { label: status.toUpperCase(), cls: "bgr", message: message || null, isFailure: false };
  return { ...known, message: message || null };
}
