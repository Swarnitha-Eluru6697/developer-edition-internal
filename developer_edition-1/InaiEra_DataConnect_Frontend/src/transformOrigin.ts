/*
 * How a transform policy (an "SMT" in the UI) was made: "custom" — created on the Transforms page with "+ New SMT"; "auto" — created by the pipeline form
 * while a pipeline was being built. The backend records it (origin); a server that does not send it yet is read from the description the pipeline form
 * has always written, so the Type column and the Audit label are right either way.
 */
export type SmtOrigin = "custom" | "auto";

/** The description the pipeline form gives the policy it creates (and what the 0054 migration looks for in existing rows). */
export const AUTO_SMT_DESCRIPTION = "Auto-created from selected source transformations";

// What the UI calls them. The stored value stays "auto" (API, database, audit history); "Standard" is the name for an SMT a pipeline made for itself.
export const SMT_TYPE_LABEL: Record<SmtOrigin, string> = { custom: "Custom", auto: "Standard" };

export function smtOriginOf(policy: { origin?: string | null; description?: string | null } | null | undefined): SmtOrigin {
  if (policy?.origin === "auto" || policy?.origin === "custom") return policy.origin;
  return policy?.description === AUTO_SMT_DESCRIPTION ? "auto" : "custom";
}
