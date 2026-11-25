/** Which form field the backend's error names ({detail: {details: {field}}}), so the message can sit under that field. */
import { asApiError, apiErrorMessage } from "../../api/errors";

export function errorField(e: unknown): string | null {
  const detail = asApiError(e).data?.detail as { details?: { field?: unknown } } | string | undefined;
  const field = detail && typeof detail === "object" ? detail.details?.field : undefined;
  return typeof field === "string" ? field : null;
}

export const errorText = (e: unknown, fallback: string): string => apiErrorMessage(e, fallback, { prefer: "detail" });
