/*
 * FE-GAP-02: what a `catch` block, an RTK Query `.unwrap()` rejection or a query `error` actually is — `unknown`. These helpers turn
 * one into the text, the status or the field-level error a screen shows, so no handler needs `catch (e: any)` or `(error as any).message`.
 *
 * IMPORTANT: a rejection from the store is a plain `ApiErrorShape` OBJECT (store/apiSlice.ts's toQueryError), not an `ApiError`
 * instance, so `instanceof ApiError` is the wrong test in UI code. `apiErrorShape` recognises the shape — which an ApiError also has.
 */
import { isRecord } from "./json";
import type { ApiFieldError } from "./api";

/** What every failed request looks like to a screen: mirrors ApiError (src/api.ts) and the backend's ErrorEnvelope (app/schemas/errors.py). */
export interface ApiErrorShape {
  status: number;
  message: string;
  code?: string;
  fieldErrors?: ApiFieldError[];
  requestId?: string;
  /** Always has a value (defaults to false) so a page can render "Retry" only when it is actually true. */
  retryable: boolean;
  details?: Record<string, unknown>;
}

function fieldErrorsOf(value: unknown): ApiFieldError[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.flatMap((entry): ApiFieldError[] =>
    isRecord(entry) && typeof entry.field === "string" && typeof entry.message === "string" ? [{ field: entry.field, message: entry.message }] : []);
}

/** The error as a typed shape, if it is one (an ApiError, or what the store rejects with): a numeric `status` and a string `message`. */
export function apiErrorShape(error: unknown): ApiErrorShape | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const e = error as { status?: unknown; message?: unknown; code?: unknown; fieldErrors?: unknown; requestId?: unknown; retryable?: unknown; details?: unknown };
  if (typeof e.status !== "number" || typeof e.message !== "string") return undefined;
  return {
    status: e.status,
    message: e.message,
    code: typeof e.code === "string" ? e.code : undefined,
    fieldErrors: fieldErrorsOf(e.fieldErrors),
    requestId: typeof e.requestId === "string" ? e.requestId : undefined,
    retryable: e.retryable === true,
    details: isRecord(e.details) ? e.details : undefined,
  };
}

/** A human-readable message for anything thrown or rejected; never throws itself. */
export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  if (typeof error === "object" && error !== null && "message" in error) {
    const message = (error as { message: unknown }).message;
    if (typeof message === "string") return message;
  }
  return String(error);
}

/** The first field-level validation error the backend reported, if the failure carried any. */
export function firstFieldError(error: unknown): ApiFieldError | undefined {
  return apiErrorShape(error)?.fieldErrors?.[0];
}
